"""Trace API endpoints — turn-level inspection for the proxy.

Provides read-only access to in-memory turn trace records. These endpoints
complement the existing /sessions admin endpoints with request-level detail:
what was received, what was rewritten, what was extracted, and why.

Also includes the Fact Graph Explorer endpoints for session-scoped graph
inspection: facts by turn, invalidation chains, touched files/decisions,
recall hits, and filtering.
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


# ---------------------------------------------------------------------------
# Fact Graph Explorer endpoints
# ---------------------------------------------------------------------------


def _neo4j_ready(request: Request) -> bool:
    """Check if Neo4j is available on this app instance."""
    return getattr(request.app.state, "neo4j_ready", False)


@router.get("/graph/{session_id}/facts")
async def graph_facts(
    request: Request,
    session_id: str,
    fact_type: str | None = None,
    min_confidence: float | None = None,
    from_turn: int | None = None,
    to_turn: int | None = None,
    include_invalidated: bool = False,
    limit: int = 100,
) -> dict:
    """List facts for a session with optional filtering.

    Query params:
    - fact_type: filter by fact type (observation, preference, procedure, etc.)
    - min_confidence: minimum confidence threshold (0.0-1.0)
    - from_turn: minimum source turn (inclusive)
    - to_turn: maximum source turn (inclusive)
    - include_invalidated: include facts that have been superseded (default false)
    - limit: max facts to return (default 100)
    """
    if not _neo4j_ready(request):
        return JSONResponse(status_code=503, content={"error": "Neo4j not available"})

    try:
        from src.graph.repository import CONTEXT_SESSION_LABEL, run_query

        valid_filter = "" if include_invalidated else "WHERE f.valid_until IS NULL"
        type_filter = "AND f.fact_type = $fact_type" if fact_type else ""
        confidence_filter = "AND f.confidence >= $min_confidence" if min_confidence is not None else ""
        from_turn_filter = "AND f.source_turn >= $from_turn" if from_turn is not None else ""
        to_turn_filter = "AND f.source_turn <= $to_turn" if to_turn is not None else ""

        # Build WHERE clause — handle initial WHERE vs AND
        conditions = []
        if not include_invalidated:
            conditions.append("f.valid_until IS NULL")
        if fact_type:
            conditions.append("f.fact_type = $fact_type")
        if min_confidence is not None:
            conditions.append("f.confidence >= $min_confidence")
        if from_turn is not None:
            conditions.append("f.source_turn >= $from_turn")
        if to_turn is not None:
            conditions.append("f.source_turn <= $to_turn")

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        cypher = f"""
            MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
            {where_clause}
            RETURN f ORDER BY f.source_turn ASC LIMIT $limit
        """

        params: dict = {"session_id": session_id, "limit": limit}
        if fact_type:
            params["fact_type"] = fact_type
        if min_confidence is not None:
            params["min_confidence"] = min_confidence
        if from_turn is not None:
            params["from_turn"] = from_turn
        if to_turn is not None:
            params["to_turn"] = to_turn

        results = await run_query(cypher, params)
        facts = [r["f"] for r in results]
        return {"session_id": session_id, "facts": facts, "count": len(facts)}
    except Exception as e:
        logger.warning("graph_facts_query_failed", session_id=session_id, error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Graph query failed: {e}"})


@router.get("/graph/{session_id}/invalidations")
async def graph_invalidation_chains(request: Request, session_id: str) -> dict:
    """Show supersession / invalidation chains for a session.

    Returns facts that have been invalidated, grouped by which fact
    superseded them. This reveals how knowledge evolved over the session.
    """
    if not _neo4j_ready(request):
        return JSONResponse(status_code=503, content={"error": "Neo4j not available"})

    try:
        from src.graph.repository import CONTEXT_SESSION_LABEL, run_query

        # Find SUPERSEDES chains
        cypher = f"""
            MATCH (new:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
                  -[:SUPERSEDES]->(old:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
            RETURN new.fact_id AS new_id, new.content AS new_content,
                   new.source_turn AS new_turn, new.fact_type AS new_type,
                   old.fact_id AS old_id, old.content AS old_content,
                   old.source_turn AS old_turn, old.fact_type AS old_type
            ORDER BY new.source_turn ASC
        """
        results = await run_query(cypher, {"session_id": session_id})

        chains = []
        for r in results:
            chains.append({
                "superseding_fact": {
                    "fact_id": r["new_id"],
                    "content": r["new_content"],
                    "source_turn": r["new_turn"],
                    "fact_type": r["new_type"],
                },
                "superseded_fact": {
                    "fact_id": r["old_id"],
                    "content": r["old_content"],
                    "source_turn": r["old_turn"],
                    "fact_type": r["old_type"],
                },
            })

        # Also find invalidated facts without explicit SUPERSEDES edges
        invalidated_cypher = f"""
            MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
            WHERE f.valid_until IS NOT NULL
            RETURN f.fact_id AS fact_id, f.content AS content,
                   f.source_turn AS source_turn, f.fact_type AS fact_type,
                   f.invalidated_at AS invalidated_at
            ORDER BY f.source_turn ASC
        """
        inv_results = await run_query(invalidated_cypher, {"session_id": session_id})

        invalidated = [
            {
                "fact_id": r["fact_id"],
                "content": r["content"],
                "source_turn": r["source_turn"],
                "fact_type": r["fact_type"],
                "invalidated_at": str(r["invalidated_at"]) if r.get("invalidated_at") else None,
            }
            for r in inv_results
        ]

        return {
            "session_id": session_id,
            "supersession_chains": chains,
            "chain_count": len(chains),
            "invalidated_facts": invalidated,
            "invalidated_count": len(invalidated),
        }
    except Exception as e:
        logger.warning("graph_invalidation_query_failed", session_id=session_id, error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Graph query failed: {e}"})


@router.get("/graph/{session_id}/files")
async def graph_touched_files(request: Request, session_id: str) -> dict:
    """Show files touched by a session (via TOUCHES edges)."""
    if not _neo4j_ready(request):
        return JSONResponse(status_code=503, content={"error": "Neo4j not available"})

    try:
        from src.graph.repository import CONTEXT_SESSION_LABEL, run_query

        cypher = f"""
            MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
                  -[:TOUCHES]->(f:{CONTEXT_SESSION_LABEL}:File)
            RETURN f.path AS path, f.status AS status,
                   f.last_read_turn AS last_read_turn,
                   f.last_modified_turn AS last_modified_turn
            ORDER BY f.path ASC
        """
        results = await run_query(cypher, {"session_id": session_id})
        files = [
            {
                "path": r["path"],
                "status": r["status"],
                "last_read_turn": r.get("last_read_turn"),
                "last_modified_turn": r.get("last_modified_turn"),
            }
            for r in results
        ]
        return {"session_id": session_id, "files": files, "count": len(files)}
    except Exception as e:
        logger.warning("graph_files_query_failed", session_id=session_id, error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Graph query failed: {e}"})


@router.get("/graph/{session_id}/decisions")
async def graph_decisions(request: Request, session_id: str) -> dict:
    """Show decisions recorded for a session."""
    if not _neo4j_ready(request):
        return JSONResponse(status_code=503, content={"error": "Neo4j not available"})

    try:
        from src.graph.repository import CONTEXT_SESSION_LABEL, run_query

        cypher = f"""
            MATCH (d:{CONTEXT_SESSION_LABEL}:Decision {{session_id: $session_id}})
            RETURN d.decision_id AS decision_id, d.summary AS summary,
                   d.rationale AS rationale, d.turn AS turn,
                   d.superseded_by AS superseded_by
            ORDER BY d.turn ASC
        """
        results = await run_query(cypher, {"session_id": session_id})
        decisions = [
            {
                "decision_id": r["decision_id"],
                "summary": r["summary"],
                "rationale": r.get("rationale"),
                "turn": r["turn"],
                "superseded_by": r.get("superseded_by"),
            }
            for r in results
        ]
        return {"session_id": session_id, "decisions": decisions, "count": len(decisions)}
    except Exception as e:
        logger.warning("graph_decisions_query_failed", session_id=session_id, error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Graph query failed: {e}"})


@router.get("/graph/{session_id}/recall")
async def graph_recall_hits(request: Request, session_id: str) -> dict:
    """Show recall events for a session from trace records.

    This uses the in-memory trace store rather than Neo4j, since recall
    events are captured in the TurnTrace DTO.
    """
    store = get_trace_store()
    turns = await store.get_session_turns(session_id, limit=200, offset=0)

    recall_events = []
    for t in turns:
        if t.recall_used:
            recall_events.append({
                "turn": t.turn_number,
                "question": t.recall_question,
                "facts_returned": t.recall_facts_returned,
                "assembly_mode": t.assembly_mode,
            })

    return {
        "session_id": session_id,
        "recall_events": recall_events,
        "count": len(recall_events),
    }
