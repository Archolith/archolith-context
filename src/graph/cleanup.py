"""TTL-based session expiry cleanup."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from src.config import get_settings
from src.graph.repository import CONTEXT_SESSION_LABEL, run_query, run_write
from src.models.graph_nodes import SessionStatus

logger = structlog.get_logger()


async def expire_sessions() -> int:
    """Mark sessions past their TTL as expired."""
    settings = get_settings()
    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{status: 'active'}})
WHERE duration.between(s.last_active, datetime()).hours > $ttl_hours
    SET s.status = 'expired'
    RETURN count(s) AS expired
    """
    results = await run_write(cypher, {"ttl_hours": settings.session_ttl_hours})
    count = results[0]["expired"] if results else 0
    if count:
        logger.info("sessions_expired", count=count)
    return count


async def delete_expired_sessions() -> int:
    """Delete all nodes/edges for expired sessions."""
    cypher = f"""
    MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{status: 'expired'}})
    WITH s MATCH (s)-[r*0..]-(n:{CONTEXT_SESSION_LABEL})
    WHERE n.session_id = s.session_id
    DETACH DELETE n
    RETURN count(DISTINCT s) AS deleted
    """
    results = await run_write(cypher, {})
    count = results[0]["deleted"] if results else 0
    if count:
        logger.info("expired_sessions_deleted", count=count)
    return count


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
