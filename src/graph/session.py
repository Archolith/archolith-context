"""Session CRUD operations with :ContextSession label isolation."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from src.graph.repository import CONTEXT_SESSION_LABEL, run_query, run_write
from src.models.graph_nodes import SessionStatus

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
    """Look up a session by fingerprint."""
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{fingerprint: $fingerprint}})
RETURN s
"""
    results = await run_query(cypher, {"fingerprint": fingerprint})
    return results[0]["s"] if results else None


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
