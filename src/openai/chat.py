""" /v1/chat/completions endpoint — proxy with session resolution, context assembly, and extraction."""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
import structlog
from fastapi import APIRouter, Request
from starlette.background import BackgroundTasks
from starlette.responses import Response, StreamingResponse

from src.assembler.context import assemble_context
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

# Transient status codes eligible for retry
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Pattern for stripping model reasoning/thinking blocks before extraction
_REASONING_PATTERN = re.compile(
    r'<(?:thinking|reasoning|inner_monologue)>.*?</(?:thinking|reasoning|inner_monologue)>',
    re.DOTALL,
)


def _strip_reasoning(text: str) -> str:
    """Strip model reasoning blocks before extraction.

    Models that emit <thinking>/<reasoning> blocks include internal scaffolding
    (tentative reasoning, abandoned approaches, self-corrections) that isn't useful
    as facts. Stripping prevents noise in the extraction pipeline.
    """
    return _REASONING_PATTERN.sub('', text).strip()


def _estimate_input_tokens(messages: list[dict]) -> int:
    """Estimate total input tokens using tiktoken cl100k_base with 10% margin + 500 floor."""
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    total_tokens = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multi-part content
            for part in content:
                if isinstance(part, dict):
                    total_tokens += len(enc.encode(part.get("text", "")))
        elif isinstance(content, str):
            total_tokens += len(enc.encode(content))
    with_margin = int(total_tokens * 1.10)
    return max(with_margin, 500)


def _rewrite_messages(
    original_messages: list[dict],
    assembled_context: "AssembledContext",
    coherence_tail_size: int,
    max_tail_messages: int = 20,
) -> list[dict]:
    """Rewrite the messages array: merge graph context into system prompt + coherence tail.

    Strategy:
    1. Merge graph-assembled context INTO the original system message
       (NVIDIA API rejects multiple consecutive system messages)
    2. Keep the last N messages as the "coherence tail" (recent context the model needs)
    3. Discard the middle messages (replaced by graph context)

    This reduces a 100K+ token linear history to ~15-20K of curated context.
    """
    if not assembled_context or not assembled_context.graph_context:
        return original_messages

    result = []

    # 1. Merge graph context into the original system message
    system_msg = None
    rest = []
    for msg in original_messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = msg.copy()
        else:
            rest.append(msg)

    # Build the combined system message: original + graph context
    graph_content = "\n\n".join(
        m.get("content", "") for m in assembled_context.graph_context
    )
    if system_msg:
        system_msg["content"] = system_msg.get("content", "") + "\n\n" + graph_content
        result.append(system_msg)
    else:
        # No original system message — graph context becomes the system message
        result.append({"role": "system", "content": graph_content})

    # 2. Keep the coherence tail — use smart_tail to preserve tool-call integrity
    from src.assembler.tail import smart_tail
    tail = smart_tail(rest, base_size=coherence_tail_size, max_size=max_tail_messages)

    # 3. Ensure role alternation: after system messages, the first non-system
    # message must be 'user'. Strip any leading assistant/tool messages.
    while tail and tail[0].get("role") not in ("user",):
        tail = tail[1:]

    # 4. Validate alternation: merge any consecutive duplicate roles
    validated_tail = []
    for msg in tail:
        if validated_tail:
            prev_role = validated_tail[-1].get("role")
            curr_role = msg.get("role")
            if prev_role == "user" and curr_role == "user":
                prev_content = validated_tail[-1].get("content", "")
                curr_content = msg.get("content", "")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    validated_tail[-1]["content"] = prev_content + "\n\n" + curr_content
                    continue
            if prev_role == "assistant" and curr_role == "assistant":
                prev_content = validated_tail[-1].get("content", "")
                curr_content = msg.get("content", "")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    validated_tail[-1]["content"] = prev_content + "\n\n" + curr_content
                    continue
        validated_tail.append(msg)

    result.extend(validated_tail)

    return result


def _record_assembly_mode(mode: str) -> None:
    """Record assembly mode in process-level metrics."""
    from src.main import _metrics
    if mode in _metrics["assembly_modes"]:
        _metrics["assembly_modes"][mode] += 1


async def _upstream_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict,
    content: bytes,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> httpx.Response:
    """Send request to upstream with exponential backoff on transient errors."""
    from src.main import _metrics
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = await client.post(url, headers=headers, content=content)
            if resp.status_code not in _RETRYABLE_STATUS_CODES:
                return resp
            # Retryable status code
            if attempt < max_retries - 1:
                delay = backoff_base * (2 ** attempt)
                logger.warning(
                    "upstream_retryable_error",
                    status=resp.status_code,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
            else:
                return resp  # Last attempt, return whatever we got
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = backoff_base * (2 ** attempt)
                logger.warning(
                    "upstream_connection_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay_s=delay,
                    error=str(e),
                )
                await asyncio.sleep(delay)
            else:
                _metrics["upstream_errors"] += 1
                raise
    # Should not reach here, but just in case
    _metrics["upstream_errors"] += 1
    raise last_exc or httpx.ConnectError("All retry attempts exhausted")


@router.post("/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Accept OpenAI chat completion requests, forward to upstream,
    resolve session, assemble context, and trigger async fact extraction."""
    from src.main import _metrics
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

    # Guard: if lifespan didn't initialize http_client, return 503
    if not hasattr(request.app.state, "http_client"):
        return make_error_response(503, "Proxy not initialized — lifespan did not complete", "server_error")

    if neo4j_ready:
        try:
            headers = {k: v for k, v in request.headers.items()}
            messages_raw = body.get("messages", [])
            session_id, is_new = await resolve_session(headers, messages_raw)
            turn_number = await session_repo.get_turn_number(session_id)
            logger.debug("session_resolved", session_id=session_id, turn=turn_number, is_new=is_new)
            # Bind session context for request-level logging middleware
            structlog.contextvars.bind_contextvars(
                session_id=session_id,
                turn_number=turn_number,
            )
        except Exception as e:
            logger.warning("session_resolution_failed", error=str(e))
            _metrics["neo4j_errors"] += 1

    # Context assembly — rewrite messages if graph has enough data
    assembled = None
    assembly_mode = "passthrough"
    input_tokens = _estimate_input_tokens(body.get("messages", []))
    _metrics["total_input_tokens_seen"] += input_tokens

    if session_id and neo4j_ready:
        try:
            # Wait for prior turn's extraction to commit before reading graph state
            from src.proxy.locks import wait_for_prior_extraction
            await wait_for_prior_extraction(session_id, timeout_s=5.0)

            # Extract the current user message for embedding-based retrieval
            user_message = None
            for msg in reversed(body.get("messages", [])):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "") for p in content if isinstance(p, dict)
                        )
                    user_message = content[:4000]
                    break

            assembly_start = time.monotonic()
            assembled = await assemble_context(
                session_id=session_id,
                turn_number=turn_number,
                input_token_estimate=input_tokens,
                user_message=user_message,
                http_client=request.app.state.http_client if settings.embedding_enabled else None,
            )
            assembly_latency_ms = (time.monotonic() - assembly_start) * 1000

            if assembled:
                original_count = len(body.get("messages", []))
                body["messages"] = _rewrite_messages(
                    body.get("messages", []),
                    assembled,
                    settings.coherence_tail_size,
                    max_tail_messages=settings.max_tail_messages,
                )
                rewritten_count = len(body["messages"])
                assembly_mode = "graph"

                # Estimate token savings
                rewritten_tokens = _estimate_input_tokens(body["messages"])
                savings = max(0, input_tokens - rewritten_tokens)
                _metrics["token_savings_estimated"] += savings

                logger.info(
                    "messages_rewritten",
                    session_id=session_id,
                    turn=turn_number,
                    original_messages=original_count,
                    rewritten_messages=rewritten_count,
                    facts_injected=assembled.facts_retrieved,
                    token_estimate=assembled.token_estimate,
                    savings_tokens=savings,
                    assembly_latency_ms=round(assembly_latency_ms, 1),
                )

                # P99 budget check — log warning but use the assembled context
                # (assembly already completed, discarding would waste the work)
                if assembly_latency_ms > settings.assembly_latency_budget_ms:
                    logger.warning(
                        "assembly_latency_exceeded_budget",
                        latency_ms=round(assembly_latency_ms, 1),
                        budget_ms=settings.assembly_latency_budget_ms,
                    )
            else:
                # assemble_context returned None = cold start
                assembly_mode = "cold_start"
        except Exception as e:
            logger.warning("context_assembly_failed", session_id=session_id, error=str(e), exc_info=True)
            assembly_mode = "fallback"
            _metrics["neo4j_errors"] += 1
            # Fall through to passthrough — assembly failure must not block requests

    _record_assembly_mode(assembly_mode)

    # Bind assembly_mode for request-level logging middleware
    structlog.contextvars.bind_contextvars(assembly_mode=assembly_mode)

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
    """Stream SSE chunks from upstream with response capture and extraction.

    Two-phase architecture:
      1. Connection-level retry: open client.stream(), check status code.
         If 429/5xx or connection error, close and retry with backoff.
         This happens BEFORE any chunks reach the client.
      2. True SSE passthrough: once status is 200, relay aiter_lines()
         directly to the client in real-time. No buffering — the client
         sees tokens as they arrive from upstream. ResponseCapture runs
         in parallel to accumulate chunks for post-hoc extraction.

    Limitation: once SSE chunks start flowing to the client (phase 2),
    retry is impossible — the client has already consumed partial output.
    Mid-stream errors result in a broken stream.
    """
    from src.main import _metrics
    settings = get_settings()
    capture_holder = {"capture": None}

    async def stream_generator():
        # --- Phase 1: Connection-level retry ---
        # Open the stream, check status code, retry if transient error.
        # The stream context manager gives us headers (status code) before
        # we start reading the body, so we can decide to retry without
        # buffering any content.
        max_retries = settings.upstream_max_retries
        backoff_base = settings.upstream_retry_backoff_base_s
        upstream_resp = None
        stream_ctx = None  # Track the open stream context for cleanup

        for attempt in range(max_retries):
            # Close previous attempt's context if it exists
            if stream_ctx is not None:
                try:
                    await stream_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                stream_ctx = None

            try:
                stream_ctx = request.app.state.http_client.stream(
                    "POST", url, headers=headers, content=body,
                )
                resp = await stream_ctx.__aenter__()

                # Check status before committing to stream
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    # Read the small error body so we can close cleanly
                    await resp.aread()
                    # Close this attempt — context will be re-opened on next iteration
                    try:
                        await stream_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    stream_ctx = None

                    if attempt < max_retries - 1:
                        delay = backoff_base * (2 ** attempt)
                        logger.warning(
                            "streaming_connection_retry",
                            status=resp.status_code,
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay_s=delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        # Final attempt failed — yield error SSE event to client
                        _metrics["upstream_errors"] += 1
                        error = json.dumps(
                            {"error": {"message": f"Upstream returned {resp.status_code} after {max_retries} retries", "type": "upstream_error"}}
                        )
                        yield f"data: {error}\n\n"
                        return

                # Non-retryable error (e.g. 400, 401) — relay and stop
                if resp.status_code >= 400:
                    _metrics["upstream_errors"] += 1
                    error_body = await resp.aread()
                    try:
                        await stream_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    stream_ctx = None
                    yield f"data: {error_body.decode()}\n\n"
                    return

                # Good response (2xx) — keep the stream open for phase 2
                upstream_resp = resp
                break

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                # Context was never entered or already cleaned up
                stream_ctx = None

                if attempt < max_retries - 1:
                    delay = backoff_base * (2 ** attempt)
                    logger.warning(
                        "streaming_connection_retry",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay_s=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    _metrics["upstream_errors"] += 1
                    error = json.dumps(
                        {"error": {"message": f"Upstream connection failed after {max_retries} retries: {e}", "type": "upstream_error"}}
                    )
                    yield f"data: {error}\n\n"
                    return
        else:
            _metrics["upstream_errors"] += 1
            error = json.dumps(
                {"error": {"message": "All streaming connection attempts exhausted", "type": "upstream_error"}}
            )
            yield f"data: {error}\n\n"
            return

        # --- Phase 2: True SSE passthrough ---
        # Relay upstream chunks to the client in real-time via aiter_lines().
        # ResponseCapture accumulates chunks in parallel for post-hoc extraction.
        capture = None
        try:
            async for line, cap in stream_with_capture(upstream_resp):
                if cap is not None:
                    # Final yield from stream_with_capture — capture is complete
                    capture = cap
                    continue
                if line:
                    yield line + "\n\n"

        except Exception as e:
            _metrics["upstream_errors"] += 1
            logger.error("streaming_error", error=str(e), exc_info=True)
            error = json.dumps(
                {"error": {"message": f"Internal proxy error: {e}", "type": "server_error"}}
            )
            yield f"data: {error}\n\n"
        finally:
            # Always close the stream context
            if stream_ctx is not None:
                try:
                    await stream_ctx.__aexit__(None, None, None)
                except Exception:
                    pass

        capture_holder["capture"] = capture

        # Schedule extraction as a background task that runs after streaming completes
        if session_id:
            async def post_stream_extraction():
                cap = capture_holder.get("capture")
                if cap and cap.get_full_text():
                    await _run_extraction(
                        client=request.app.state.extractor_client,
                        session_id=session_id,
                        turn_number=turn_number,
                        messages=messages or [],
                        response_text=cap.get_full_text(),
                        truncated=cap.truncated,
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
    """Handle non-streaming request with retry and extraction."""
    from src.main import _metrics
    settings = get_settings()

    try:
        resp = await _upstream_request_with_retry(
            client=request.app.state.http_client,
            method="POST",
            url=url,
            headers=headers,
            content=body,
            max_retries=settings.upstream_max_retries,
            backoff_base=settings.upstream_retry_backoff_base_s,
        )
    except httpx.TimeoutException:
        _metrics["upstream_errors"] += 1
        return make_error_response(504, "Upstream request timed out", "upstream_timeout", code="timeout")
    except httpx.ConnectError as e:
        _metrics["upstream_errors"] += 1
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
    """Run fact extraction and store results in graph. Best-effort, non-blocking.

    Holds a per-session lock during graph writes so that subsequent assembly
    reads see committed state. After extraction, computes batch embeddings
    for all facts and stores them with their vectors. If embedding fails,
    facts are stored without embeddings (assembler falls back to recency-only
    retrieval).
    """
    from src.main import _metrics
    from src.proxy.locks import get_session_lock

    lock = get_session_lock(session_id)
    # Try to acquire with timeout — don't block forever if another extraction is stuck
    acquired = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=10.0)
        acquired = True
    except asyncio.TimeoutError:
        logger.warning("extraction_lock_acquire_timeout", session_id=session_id, turn=turn_number)
        # Proceed without lock — stale data risk, but better than blocking forever

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

        # Strip reasoning blocks from the response before extraction
        response_text = _strip_reasoning(response_text)

        # Extract tool results from messages — structured serialization with tool names
        tool_results_parts = []
        for msg in messages:
            if msg.get("role") == "tool":
                tool_name = msg.get("name", "unknown_tool")
                content = msg.get("content", "")
                if isinstance(content, str):
                    tool_results_parts.append(f"Tool [{tool_name}]:\n{content[:2000]}")
        tool_results = "\n\n".join(tool_results_parts) if tool_results_parts else None

        extraction_start = time.monotonic()
        result = await extract_facts(
            http_client=client,
            turn_number=turn_number,
            user_message=user_message[:4000],
            assistant_response=response_text[:8000],
            tool_results=tool_results[:4000] if tool_results else None,
        )
        extraction_latency_ms = (time.monotonic() - extraction_start) * 1000

        if not result or not result.facts:
            logger.info("extraction_empty", session_id=session_id, turn=turn_number)
            _metrics["extraction_successes"] += 1
            return

        # Batch compute embeddings for all extracted facts
        fact_contents = [fact.get("content", "") for fact in result.facts]
        embeddings = await _compute_fact_embeddings(client, fact_contents)

        # Store facts with their embeddings
        for i, fact in enumerate(result.facts):
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
                embedding=embeddings[i] if i < len(embeddings) else None,
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
        embedding_count = sum(1 for e in embeddings if e is not None)
        _metrics["extraction_successes"] += 1
        logger.info(
            "extraction_stored",
            session_id=session_id,
            turn=turn_number,
            facts_stored=len(result.facts),
            embeddings_computed=embedding_count,
            active_fact_count=active_count,
            extraction_latency_ms=round(extraction_latency_ms, 1),
            warning="high_active_count" if active_count > 200 else None,
        )

    except Exception as e:
        _metrics["extraction_failures"] += 1
        logger.warning("extraction_task_failed", session_id=session_id, turn=turn_number, error=str(e), exc_info=True)
    finally:
        if acquired:
            lock.release()



async def _compute_fact_embeddings(
    client: httpx.AsyncClient,
    texts: list[str],
) -> list[list[float] | None]:
    """Compute batch embeddings for extracted fact texts.

    Falls back to [None, ...] if the embedding API is unavailable.
    """
    try:
        from src.extractor.embeddings import compute_embeddings_batch
        return await compute_embeddings_batch(client, texts)
    except Exception as e:
        logger.warning("embedding_computation_failed", error=str(e))
        return [None] * len(texts)
