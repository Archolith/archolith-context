"""LadybugDB session CRUD operations."""

from __future__ import annotations

from uuid import uuid4

from archolith_proxy.config import get_settings

# Re-export _execute type — callers pass the ladybug backend's _execute


async def create_session(execute, session_id: str, fingerprint: str | None = None) -> dict:
    rows = await execute(
        """
        CREATE (s:Session {
            session_id: $session_id, fingerprint: $fingerprint,
            goal: NULL, created_at: current_timestamp(),
            last_active: current_timestamp(),
            ttl_hours: 24, status: 'active', turn_number: 0
        }) RETURN s
        """,
        {"session_id": session_id, "fingerprint": fingerprint},
    )
    return rows[0] if rows else {}


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
