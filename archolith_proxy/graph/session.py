"""Session CRUD operations with :ContextSession label isolation."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from archolith_proxy.graph.repository import CONTEXT_SESSION_LABEL, run_query, run_write
from archolith_proxy.models.graph_nodes import SessionStatus

logger = structlog.get_logger()


async def create_session(session_id: str, fingerprint: str | None = None) -> dict:
    """Create a new session node."""
    now = datetime.now(timezone.utc).isoformat()
    cypher = f"""
CREATE (s:{CONTEXT_SESSION_LABEL}:Session {{
  session_id: $session_id,
  fingerprint: $fingerprint,
  goal: null,
  created_at: datetime($now),
  last_active: datetime($now),
  ttl_hours: 24,
  status: $status,
  turn_number: 0
}})
RETURN s
"""
    results = await run_write(cypher, {
        "session_id": session_id,
        "fingerprint": fingerprint,
        "now": now,
        "status": SessionStatus.ACTIVE.value,
    })
    return results[0]["s"] if results else {}


async def find_by_session_id(session_id: str) -> dict | None:
    """Look up a session by session_id."""
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
RETURN s
"""
    results = await run_query(cypher, {"session_id": session_id})
    return results[0]["s"] if results else None


async def find_by_fingerprint(fingerprint: str) -> dict | None:
    """Look up a session by fingerprint.

    Returns the session node if found, None otherwise.
    """
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{fingerprint: $fingerprint}})
RETURN s
"""
    results = await run_query(cypher, {"fingerprint": fingerprint})
    return results[0]["s"] if results else None


async def find_or_create_by_fingerprint(
    fingerprint: str,
) -> tuple[dict, bool]:
    """Atomically find or create a session by fingerprint.

    Uses MERGE to avoid lookup-then-create races when two concurrent
    requests arrive with the same fingerprint. Returns (session_data, is_new).
    """
    import uuid
    from archolith_proxy.models.graph_nodes import SessionStatus

    now = datetime.now(timezone.utc).isoformat()
    session_id = uuid.uuid4().hex[:16]

    cypher = f"""
    MERGE (s:{CONTEXT_SESSION_LABEL}:Session {{fingerprint: $fingerprint}})
    ON CREATE SET
        s.session_id = $session_id,
        s.goal = null,
        s.created_at = datetime($now),
        s.last_active = datetime($now),
        s.ttl_hours = 24,
        s.status = $status,
        s.turn_number = 0
    ON MATCH SET
        s.last_active = datetime($now)
    RETURN s, CASE WHEN s.created_at = datetime($now) THEN true ELSE false END AS is_new
    """
    results = await run_write(cypher, {
        "fingerprint": fingerprint,
        "session_id": session_id,
        "now": now,
        "status": SessionStatus.ACTIVE.value,
    })
    if results:
        return results[0]["s"], results[0].get("is_new", False)
    # Fallback: should not happen, but handle gracefully
    return {}, True


async def touch_session(session_id: str) -> None:
    """Update last_active and increment turn_number."""
    now = datetime.now(timezone.utc).isoformat()
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
SET s.last_active = datetime($now), s.turn_number = s.turn_number + 1
    """
    await run_write(cypher, {"session_id": session_id, "now": now})


async def get_turn_number(session_id: str) -> int:
    """Get current turn number for a session."""
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
RETURN s.turn_number AS turn
    """
    results = await run_query(cypher, {"session_id": session_id})
    return results[0]["turn"] if results else 0


async def update_goal(session_id: str, goal: str) -> None:
    """Update the session goal."""
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
SET s.goal = $goal
    """
    await run_write(cypher, {"session_id": session_id, "goal": goal})


async def list_active_sessions() -> list[dict]:
    """List all active sessions (for admin/metrics endpoint)."""
    cypher = f"""
    MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{status: 'active'}})
    RETURN s.session_id AS session_id,
    s.fingerprint AS fingerprint,
    s.turn_number AS turn_number,
    s.created_at AS created_at,
    s.last_active AS last_active,
    s.goal AS goal
    ORDER BY s.last_active DESC
    """
    results = await run_query(cypher, {})

    # Convert Neo4j datetime objects to ISO strings for JSON serialization
    for row in results:
        for key in ("created_at", "last_active"):
            val = row.get(key)
            if val and hasattr(val, "iso_format"):
                row[key] = val.iso_format()
            elif val and hasattr(val, "isoformat"):
                row[key] = val.isoformat()

    return results


async def get_session_stats(session_id: str) -> dict:
    """Get stats for a specific session."""
    facts_cypher = f"""
    MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
    WHERE f.valid_until IS NULL
    RETURN count(f) AS active_facts
    """
    session_cypher = f"""
    MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
    RETURN s.turn_number AS turn_number, s.goal AS goal, s.status AS status,
    s.created_at AS created_at, s.last_active AS last_active
    """
    facts = await run_query(facts_cypher, {"session_id": session_id})
    session = await run_query(session_cypher, {"session_id": session_id})

    if not session:
        return {}

    result = {**session[0], "active_facts": facts[0]["active_facts"] if facts else 0}

    # Convert Neo4j datetime objects to ISO strings for JSON serialization
    for key in ("created_at", "last_active"):
        val = result.get(key)
        if val and hasattr(val, "iso_format"):
            result[key] = val.iso_format()
        elif val and hasattr(val, "isoformat"):
            result[key] = val.isoformat()

    return result
