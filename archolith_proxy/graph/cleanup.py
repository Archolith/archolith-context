"""TTL-based session expiry cleanup."""

from __future__ import annotations

import structlog

from archolith_proxy.config import get_settings
from archolith_proxy.graph.repository import CONTEXT_SESSION_LABEL, run_write

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


async def delete_session_data(session_id: str) -> dict:
    """Delete all ContextSession-labelled nodes for a single session."""
    cypher = f"""
    MATCH (n:{CONTEXT_SESSION_LABEL} {{session_id: $session_id}})
    DETACH DELETE n
    RETURN count(n) AS deleted
    """
    results = await run_write(cypher, {"session_id": session_id})
    deleted = results[0]["deleted"] if results else 0
    if deleted:
        logger.info("session_data_deleted", session_id=session_id, nodes_deleted=deleted)
    return {"nodes_deleted": deleted}
