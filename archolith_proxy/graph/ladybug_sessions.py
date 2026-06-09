"""LadybugDB session CRUD operations."""

from __future__ import annotations

import asyncio
import base64
from uuid import uuid4

# Per-fingerprint locks serialise the find-or-create slow path so that two
# concurrent first requests for the same fingerprint cannot both observe no
# session and each create one.  Keyed on fingerprint; grows to at most the
# number of unique clients ever seen in this process lifetime.
_fingerprint_create_locks: dict[str, asyncio.Lock] = {}

# Re-export _execute type — callers pass the ladybug backend's _execute


def _encode_overrides(overrides_json: str) -> str:
    """Encode a per-session config JSON string for safe storage.

    LadybugDB 0.16.1 type-infers a STRING parameter whose value begins with '{'
    as a MAP/STRUCT and stores a mangled repr (quotes stripped, true->True), so a
    raw JSON object can never round-trip. base64 makes the stored value an opaque
    ASCII string the driver cannot reinterpret. Symmetric with _decode_overrides.

    Upstream bug: https://github.com/LadybugDB/ladybug/issues/580 — remove this
    base64 layer once the binder stops content-sniffing str params.
    """
    if not overrides_json:
        return ""
    return base64.b64encode(overrides_json.encode("utf-8")).decode("ascii")


def _decode_overrides(stored: str | None) -> str:
    """Decode a stored per-session config value back to its JSON string ('' if empty)."""
    if not stored:
        return ""
    try:
        return base64.b64decode(stored.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        # Tolerate a legacy/plain value that was stored before base64 encoding.
        return stored


async def create_session(execute, session_id: str, fingerprint: str | None = None) -> dict:
    rows = await execute(
        """
        CREATE (s:Session {
            session_id: $session_id, fingerprint: $fingerprint,
            goal: NULL, created_at: current_timestamp(),
            last_active: current_timestamp(),
            ttl_hours: 24, status: 'active', turn_number: 0,
            config_overrides: ''
        }) RETURN s
        """,
        {"session_id": session_id, "fingerprint": fingerprint},
    )
    return rows[0] if rows else {}


async def set_session_config_overrides(
    execute, session_id: str, overrides_json: str
) -> None:
    """Persist a JSON string of per-session config overrides on the Session node.

    The value is base64-encoded for storage (see _encode_overrides). No-op
    (matches nothing) if the session does not exist; the caller is responsible
    for ensuring the session was created first.
    """
    await execute(
        "MATCH (s:Session {session_id: $session_id}) "
        "SET s.config_overrides = $overrides_json",
        {"session_id": session_id, "overrides_json": _encode_overrides(overrides_json)},
    )


async def get_session_config_overrides(execute, session_id: str) -> str:
    """Return the per-session config overrides JSON string ('' if none/unknown)."""
    rows = await execute(
        "MATCH (s:Session {session_id: $session_id}) "
        "RETURN s.config_overrides AS config_overrides",
        {"session_id": session_id},
    )
    if not rows:
        return ""
    return _decode_overrides(rows[0].get("config_overrides"))


async def find_session_by_id(execute, session_id: str) -> dict | None:
    rows = await execute(
        "MATCH (s:Session {session_id: $session_id}) RETURN s",
        {"session_id": session_id},
    )
    return rows[0] if rows else None


async def find_session_by_fingerprint(execute, fingerprint: str) -> dict | None:
    rows = await execute(
        "MATCH (s:Session {fingerprint: $fingerprint}) RETURN s",
        {"fingerprint": fingerprint},
    )
    return rows[0] if rows else None


async def find_or_create_by_fingerprint(execute, fingerprint: str) -> tuple[dict, bool]:
    """Find an existing session by fingerprint or create a new one.

    Returns (session_data, is_new). Serialised per fingerprint via
    _fingerprint_create_locks to prevent duplicate session creation when two
    concurrent first requests for the same fingerprint both observe no session.
    The fast path (session already exists) skips the lock entirely.
    """
    # Fast path: session already exists — no lock needed for read.
    existing = await find_session_by_fingerprint(execute, fingerprint)
    if existing:
        return existing, False

    # Slow path: acquire per-fingerprint lock before creating.
    if fingerprint not in _fingerprint_create_locks:
        _fingerprint_create_locks[fingerprint] = asyncio.Lock()
    lock = _fingerprint_create_locks[fingerprint]

    async with lock:
        # Double-check: a concurrent waiter may have already created the session.
        existing = await find_session_by_fingerprint(execute, fingerprint)
        if existing:
            return existing, False
        session_id = uuid4().hex[:16]
        created = await create_session(execute, session_id, fingerprint=fingerprint)
        if not created:
            existing = await find_session_by_fingerprint(execute, fingerprint)
            return existing or {}, False
        return created, True


async def touch_session(execute, session_id: str) -> None:
    await execute(
        """
        MATCH (s:Session {session_id: $session_id})
        SET s.last_active = current_timestamp(), s.turn_number = s.turn_number + 1
        """,
        {"session_id": session_id},
    )


async def get_turn_number(execute, session_id: str) -> int:
    rows = await execute(
        "MATCH (s:Session {session_id: $session_id}) RETURN s.turn_number AS turn",
        {"session_id": session_id},
    )
    return rows[0]["turn"] if rows else 0


async def update_goal(execute, session_id: str, goal: str) -> None:
    await execute(
        "MATCH (s:Session {session_id: $session_id}) SET s.goal = $goal",
        {"session_id": session_id, "goal": goal},
    )


async def list_active_sessions(execute) -> list[dict]:
    return await execute(
        """
        MATCH (s:Session {status: 'active'})
        RETURN s.session_id AS session_id, s.fingerprint AS fingerprint,
               s.turn_number AS turn_number, s.created_at AS created_at,
               s.last_active AS last_active, s.goal AS goal
        ORDER BY s.last_active DESC
        """
    )


async def get_session_stats(execute, session_id: str) -> dict:
    facts = await execute(
        "MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NULL RETURN count(f) AS active_facts",
        {"session_id": session_id},
    )
    session = await execute(
        "MATCH (s:Session {session_id: $session_id}) RETURN s.turn_number AS turn_number, s.goal AS goal, s.status AS status, s.created_at AS created_at, s.last_active AS last_active",
        {"session_id": session_id},
    )
    if not session:
        return {}
    result = dict(session[0])
    result["active_facts"] = facts[0]["active_facts"] if facts else 0
    return result
