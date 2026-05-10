""" /v1/chat/completions endpoint — proxy with session resolution and extraction."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Request
from starlette.background import BackgroundTasks
from starlette.responses import Response, StreamingResponse

from src.config import get_settings
from src.extractor.client import extract_facts
from src.graph import cleanup as cleanup_repo
from src.graph import edges as edges_repo
from src.graph import facts as facts_repo
from src.graph import session as session_repo
from src.models.graph_nodes import FactType, FileStatus
from src.openai.errors import make_error_response
from src.openai.schemas import ChatCompletionRequest
from src.proxy.session import resolve_session
from src.proxy.streaming import stream_with_capture

logger = structlog.get_logger()

router = APIRouter()


@router.post("/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Accept OpenAI chat completion requests, forward to upstream,
    resolve session, and trigger async fact extraction."""
    settings = get_settings()

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        return make_error_response(400, "Invalid JSON in request body", "invalid_request_error")

    # Validate against schema
    try:
        req = ChatCompletionRequest(**body)
    except Exception as e:
        return make_error_response(400, f"Invalid request: {e}", "invalid_request_error")

    if not req.messages:
        return make_error_response(
            400, "Messages array must not be empty", "invalid_request_error", param="messages"
        )

    # Session resolution (graceful — skip if Neo4j not ready)
    session_id = None
    turn_number = 0
    neo4j_ready = getattr(request.app.state, "neo4j_ready", False)

    if neo4j_ready:
        try:
            headers = {k: v for k, v in request.headers.items()}
            messages_raw = body.get("messages", [])
            session_id, is_new = await resolve_session(headers, messages_raw)
            turn_number = await session_repo.get_turn_number(session_id)
            logger.debug("session_resolved", session_id=session_id, turn=turn_number, is_new=is_new)
        except Exception as e:
            logger.warning("session_resolution_failed", error=str(e))

    # Build upstream request
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    upstream_headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }
    request_body = json.dumps(body).encode("utf-8")

    if req.stream:
        return await _handle_streaming(
            request, background_tasks, upstream_url, upstream_headers, request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
        )
    else:
        return await _handle_non_streaming(
            request, background_tasks, upstream_url, upstream_headers, request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
        )


async def _handle_streaming(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str,
    headers: dict,
    body: bytes,
    session_id: str | None = None,
    turn_number: int = 0,
    messages: list[dict] | None = None,
) -> StreamingResponse:
    """Stream SSE chunks from upstream with response capture and extraction."""
    capture_holder = {"capture": None}

    async def stream_generator():
        try:
            async with request.app.state.http_client.stream(
                "POST", url, headers=headers, content=body
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {error_body.decode()}\n\n"
                    return

                async for line, cap in stream_with_capture(resp):
                    if cap is not None:
                        capture_holder["capture"] = cap
                        continue
                    if line:
                        yield line + "\n\n"

        except Exception as e:
            logger.error("streaming_error", error=str(e), exc_info=True)
            error = json.dumps(
                {"error": {"message": f"Internal proxy error: {e}", "type": "server_error"}}
            )
            yield f"data: {error}\n\n"

    # Schedule extraction as a background task that runs after streaming completes
    if session_id:
        async def post_stream_extraction():
            capture = capture_holder["capture"]
            if capture and capture.get_full_text():
                await _run_extraction(
                    client=request.app.state.extractor_client,
                    session_id=session_id,
                    turn_number=turn_number,
                    messages=messages or [],
                    response_text=capture.get_full_text(),
                    truncated=capture.truncated,
                )

        background_tasks.add_task(post_stream_extraction)

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
        background=background_tasks,
    )


async def _handle_non_streaming(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str,
    headers: dict,
    body: bytes,
    session_id: str | None = None,
    turn_number: int = 0,
    messages: list[dict] | None = None,
) -> Response:
    """Handle non-streaming request with extraction."""
    import httpx

    try:
        resp = await request.app.state.http_client.post(url, headers=headers, content=body)
    except httpx.TimeoutException:
        return make_error_response(504, "Upstream request timed out", "upstream_timeout", code="timeout")
    except httpx.ConnectError as e:
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    # Schedule extraction as background task
    if session_id:
        try:
            data = resp.json()
            response_text = ""
            choices = data.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                response_text = msg.get("content", "") or ""

            if response_text:
                background_tasks.add_task(
                    _run_extraction,
                    client=request.app.state.extractor_client,
                    session_id=session_id,
                    turn_number=turn_number,
                    messages=messages or [],
                    response_text=response_text,
                )
        except Exception as e:
            logger.warning("non_streaming_extraction_setup_failed", error=str(e))

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type="application/json",
        background=background_tasks,
    )


async def _run_extraction(
    client,
    session_id: str,
    turn_number: int,
    messages: list[dict],
    response_text: str,
    truncated: bool = False,
) -> None:
    """Run fact extraction and store results in graph. Best-effort, non-blocking."""
    try:
        # Extract user message (last user message in the request)
        user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                user_message = content
                break

        if not user_message and not response_text:
            return

        # Extract tool results from messages
        tool_results = ""
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str):
                    tool_results += content[:2000] + "\n"

        result = await extract_facts(
            http_client=client,
            turn_number=turn_number,
            user_message=user_message[:4000],
            assistant_response=response_text[:8000],
            tool_results=tool_results[:4000] if tool_results else None,
        )

        if not result or not result.facts:
            logger.info("extraction_empty", session_id=session_id, turn=turn_number)
            return

        # Store facts
        for fact in result.facts:
            content = fact.get("content", "")
            fact_type_str = fact.get("fact_type", "observation")
            try:
                fact_type = FactType(fact_type_str)
            except ValueError:
                fact_type = FactType.OBSERVATION

            await facts_repo.store_fact(
                session_id=session_id,
                content=content,
                fact_type=fact_type,
                source_turn=turn_number,
                confidence=fact.get("confidence", 0.5),
            )

        # Store file touches
        for file_path in result.files_touched:
            status = FileStatus.MODIFIED  # Default — extraction doesn't always distinguish
            await edges_repo.create_touches(session_id, file_path, status, turn_number)

        # Store decisions
        for decision in result.decisions:
            await edges_repo.store_decision(
                session_id=session_id,
                summary=decision.get("summary", ""),
                rationale=decision.get("rationale"),
                turn=turn_number,
            )

        # Invalidate superseded facts
        if result.invalidated_fact_ids:
            count = await facts_repo.invalidate_facts(result.invalidated_fact_ids)
            if count:
                logger.info("facts_invalidated", count=count, session_id=session_id, turn=turn_number)

        # Log active fact count for monitoring
        active_count = await facts_repo.get_active_fact_count(session_id)
        logger.info(
            "extraction_stored",
            session_id=session_id,
            turn=turn_number,
            facts_stored=len(result.facts),
            active_fact_count=active_count,
            warning="high_active_count" if active_count > 200 else None,
        )

    except Exception as e:
        logger.warning("extraction_task_failed", session_id=session_id, turn=turn_number, error=str(e), exc_info=True)
