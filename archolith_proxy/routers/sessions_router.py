"""Session listing and detail admin endpoints."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from archolith_proxy.admin import require_admin_token
from archolith_proxy.graph.backend import is_graph_ready

logger = structlog.get_logger()

router = APIRouter()


@router.get("/sessions")
async def list_sessions(admin: None = Depends(require_admin_token)) -> dict:
    """List all active sessions (admin endpoint)."""
    if not is_graph_ready():
        return JSONResponse(status_code=503, content={"error": "Neo4j not available"})
    try:
        from archolith_proxy.graph.session import list_active_sessions

        sessions = await list_active_sessions()
        return {"sessions": sessions, "count": len(sessions)}
    except Exception as e:
        logger.warning("sessions_list_failed", error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Neo4j error: {e}"})


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str, admin: None = Depends(require_admin_token)
) -> dict:
    """Get session details (admin endpoint)."""
    if not is_graph_ready():
        return JSONResponse(status_code=503, content={"error": "Neo4j not available"})
    try:
        from archolith_proxy.graph.session import get_session_stats

        stats = await get_session_stats(session_id)
        if not stats:
            return JSONResponse(
                status_code=404, content={"error": f"Session {session_id} not found"}
            )
        return {"session_id": session_id, **stats}
    except Exception as e:
        logger.warning("session_stats_failed", session_id=session_id, error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Neo4j error: {e}"})
