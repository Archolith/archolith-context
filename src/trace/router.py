"""Trace API endpoints — turn-level inspection for the proxy.

Provides read-only access to in-memory turn trace records. These endpoints
complement the existing /sessions admin endpoints with request-level detail:
what was received, what was rewritten, what was extracted, and why.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.trace.store import get_trace_store

logger = structlog.get_logger()

router = APIRouter(prefix="/trace", tags=["trace"])


@router.get("/sessions")
async def trace_list_sessions() -> dict:
    """List all sessions that have trace records."""
    store = get_trace_store()
    summaries = await store.list_sessions()
    return {
        "sessions": [s.model_dump() for s in summaries],
        "count": len(summaries),
    }


@router.get("/sessions/{session_id}")
async def trace_get_session(session_id: str, limit: int = 50, offset: int = 0) -> dict:
    """Get trace summary and turns for a session."""
    store = get_trace_store()
    summary = await store.get_session_summary(session_id)
    if not summary:
        return JSONResponse(status_code=404, content={"error": f"No traces for session {session_id}"})

    # Enrich summary with session goal from graph if available
    try:
        from src.graph import session as session_repo
        session_data = await session_repo.find_by_session_id(session_id)
        if session_data:
            goal = session_data.get("goal")
            if goal:
                summary.goal = goal
    except Exception:
        pass  # Non-critical — proceed without goal enrichment

    turns = await store.get_session_turns(session_id, limit=limit, offset=offset)
    return {
        "summary": summary.model_dump(),
        "turns": [t.model_dump() for t in turns],
        "turn_count": len(turns),
    }


@router.get("/turns/{turn_id}")
async def trace_get_turn(turn_id: str) -> dict:
    """Get a single turn trace by its turn_id."""
    store = get_trace_store()
    trace = await store.get_turn(turn_id)
    if not trace:
        return JSONResponse(status_code=404, content={"error": f"Turn {turn_id} not found"})
    return trace.model_dump()
