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
        from src.graph import facts as facts_repo

        facts = await facts_repo.get_facts_filtered(
            session_id=session_id,
            fact_type=fact_type,
            min_confidence=min_confidence,
            from_turn=from_turn,
            to_turn=to_turn,
            include_invalidated=include_invalidated,
            limit=limit,
        )
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
        from src.graph import facts as facts_repo

        chains = await facts_repo.get_supersession_chain(session_id)
        invalidated = await facts_repo.get_invalidated_facts(session_id)

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
        from src.graph import edges as edges_repo

        results = await edges_repo.get_touched_files(session_id)
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
        from src.graph import decisions as decisions_repo

        decisions = await decisions_repo.get_decisions(session_id, include_superseded=True)
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


# ---------------------------------------------------------------------------
# Extraction QA Workbench
# ---------------------------------------------------------------------------


@router.post("/qa/extract")
async def qa_extract(request: Request) -> dict:
    """Extraction QA workbench — run extraction against sample input.

    This endpoint lets operators evaluate extraction prompt changes against
    real examples without replaying the full proxy. It does NOT write to
    the graph — it only returns the diagnostic output.

    Request body (JSON):
    - user_message: The user message from the turn
    - assistant_response: The assistant's response
    - tool_results: Optional tool results string
    - session_goal: Optional session goal for context
    - turn_number: Optional turn number (default 0)
    - session_id: Optional session_id to run dedup/invalidation against
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    user_message = body.get("user_message", "")
    assistant_response = body.get("assistant_response", "")
    if not user_message and not assistant_response:
        return JSONResponse(
            status_code=400,
            content={"error": "At least one of user_message or assistant_response is required"},
        )

    tool_results = body.get("tool_results")
    session_goal = body.get("session_goal")
    turn_number = body.get("turn_number", 0)
    session_id = body.get("session_id")

    # Step 1: Run extraction
    try:
        from src.config import get_settings
        settings = get_settings()
        extractor_client = getattr(request.app.state, "extractor_client", None)
        if not extractor_client:
            return JSONResponse(status_code=503, content={"error": "Extractor client not available"})

        from src.extractor.client import extract_facts
        import time

        start = time.monotonic()
        result = await extract_facts(
            http_client=extractor_client,
            turn_number=turn_number,
            user_message=user_message,
            assistant_response=assistant_response,
            tool_results=tool_results,
            session_goal=session_goal,
        )
        extraction_latency_ms = (time.monotonic() - start) * 1000

        if result is None:
            return {
                "status": "extraction_failed",
                "extraction_latency_ms": round(extraction_latency_ms, 1),
                "raw_result": None,
                "normalized": None,
                "dedup": None,
                "invalidation_candidates": None,
                "graph_write_set": None,
            }

    except Exception as e:
        logger.warning("qa_extract_failed", error=str(e))
        return JSONResponse(status_code=503, content={"error": f"Extraction call failed: {e}"})

    # Step 2: Normalize the result
    normalized = result.model_dump()

    # Step 3: Run dedup check if session_id provided
    dedup_info = None
    if session_id and _neo4j_ready(request):
        try:
            from src.extractor.dedup import deduplicate_facts
            from src.graph.facts import get_active_facts

            existing_facts = await get_active_facts(session_id, limit=200)
            before_count = len(result.facts)
            kept_facts = deduplicate_facts(result.facts, existing_facts)
            skipped_count = before_count - len(kept_facts)

            dedup_info = {
                "existing_active_facts": len(existing_facts),
                "new_facts_before_dedup": before_count,
                "new_facts_after_dedup": len(kept_facts),
                "duplicates_skipped": skipped_count,
                "kept_facts": kept_facts,
            }
        except Exception as e:
            dedup_info = {"error": f"Dedup check failed: {e}"}

    # Step 4: Run invalidation matching if session_id provided
    invalidation_info = None
    if session_id and _neo4j_ready(request) and result.invalidated_fact_ids:
        try:
            from src.graph.facts import find_matching_fact_ids

            matched_ids = await find_matching_fact_ids(session_id, result.invalidated_fact_ids)
            invalidation_info = {
                "invalidation_descriptions": result.invalidated_fact_ids,
                "matched_fact_ids": matched_ids,
                "match_count": len(matched_ids),
                "description_count": len(result.invalidated_fact_ids),
            }
        except Exception as e:
            invalidation_info = {"error": f"Invalidation matching failed: {e}"}
    elif result.invalidated_fact_ids:
        invalidation_info = {
            "invalidation_descriptions": result.invalidated_fact_ids,
            "matched_fact_ids": [],
            "note": "No session_id provided — cannot match to actual fact IDs",
        }

    # Step 5: Estimate graph write set
    graph_write_set = {
        "facts_to_store": len(result.facts) if result.facts else 0,
        "files_to_touch": len(result.files_touched) if result.files_touched else 0,
        "decisions_to_store": len(result.decisions) if result.decisions else 0,
        "invalidations_to_attempt": len(result.invalidated_fact_ids) if result.invalidated_fact_ids else 0,
    }
    if dedup_info and "duplicates_skipped" in dedup_info:
        graph_write_set["facts_after_dedup"] = graph_write_set["facts_to_store"] - dedup_info["duplicates_skipped"]
    if invalidation_info and "match_count" in invalidation_info:
        graph_write_set["invalidations_matched"] = invalidation_info["match_count"]

    return {
        "status": "success",
        "extraction_latency_ms": round(extraction_latency_ms, 1),
        "raw_result": normalized,
        "normalized": normalized,
        "dedup": dedup_info,
        "invalidation_candidates": invalidation_info,
        "graph_write_set": graph_write_set,
    }
