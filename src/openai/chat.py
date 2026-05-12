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
from src.proxy.streaming import ResponseCapture, stream_with_capture, stream_with_recall_detection, _assemble_streaming_response, _non_streaming_to_sse

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
                messages=body.get("messages", []),
            )
            assembly_latency_ms = (time.monotonic() - assembly_start) * 1000

            if assembled:
                original_count = len(body.get("messages", []))
                original_messages = body["messages"][:]  # Save for compaction re-rewrite
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

                # Context-overflow compaction: if rewritten payload still exceeds
                # budget, try LLM compaction of the graph context block
                if (
                    settings.compaction_enabled
                    and rewritten_tokens > settings.context_token_budget
                    and assembled
                ):
                    try:
                        from src.assembler.compaction import compact_context

                        graph_content = assembled.system_message.get("content", "")
                        target_tokens = settings.context_token_budget // 2
                        compacted = await compact_context(
                            request.app.state.http_client,
                            context_block=graph_content,
                            target_tokens=target_tokens,
                        )
                        if compacted:
                            # Replace graph context with compacted version
                            assembled.system_message["content"] = compacted
                            # Re-rewrite with compacted context
                            body["messages"] = _rewrite_messages(
                                original_messages,
                                assembled,
                                settings.coherence_tail_size,
                                max_tail_messages=settings.max_tail_messages,
                            )
                            rewritten_tokens = _estimate_input_tokens(body["messages"])
                            savings = max(0, input_tokens - rewritten_tokens)
                            logger.info(
                                "context_compaction_applied",
                                session_id=session_id,
                                turn=turn_number,
                                rewritten_tokens=rewritten_tokens,
                                budget=settings.context_token_budget,
                            )
                            _metrics["compaction_applied"] += 1
                    except Exception as e:
                        logger.warning(
                            "context_compaction_failed",
                            session_id=session_id,
                            turn=turn_number,
                            error=str(e),
                        )
                        # Compaction failed — keep the oversized assembled context
                        # (better than passthrough, which would send full history)

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

    # Inject session recall tool if enabled and session is active
    recall_injected = False
    if settings.session_recall_tool_enabled and session_id:
        from src.proxy.tool_injection import inject_recall_tool
        body = inject_recall_tool(body)
        recall_injected = True

    request_body = json.dumps(body).encode("utf-8")

    if req.stream:
        return await _handle_streaming(
            request, background_tasks, upstream_url, upstream_headers, request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
            recall_injected=recall_injected,
        )
    else:
        return await _handle_non_streaming(
            request, background_tasks, upstream_url, upstream_headers, request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
            recall_injected=recall_injected,
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
    recall_injected: bool = False,
) -> StreamingResponse:
    """Stream SSE chunks from upstream with response capture and extraction.

    Three-phase architecture:
    1. Connection-level retry: open client.stream(), check status code.
       If 429/5xx or connection error, close and retry with backoff.
       This happens BEFORE any chunks reach the client.
    2. True SSE passthrough: once status is 200, relay aiter_lines()
       directly to the client in real-time. No buffering — the client
       sees tokens as they arrive from upstream. ResponseCapture runs
       in parallel to accumulate chunks for post-hoc extraction.
    2b. If recall tool is injected: buffer-and-decide — detect if the
       model calls __context_engine_recall in the stream, intercept,
       execute recall, re-send non-streaming, then convert the second
       response to SSE format and relay to client.

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

        # --- Phase 2: SSE passthrough with optional recall detection ---
        capture = None
        recall_intercepted = False
        try:
            if recall_injected and session_id:
                # Buffer-and-decide mode: detect recall tool calls in stream
                from src.proxy.tool_injection import (
                    RECALL_TOOL_NAME,
                    find_recall_tool_call,
                    handle_recall_tool_call,
                    build_tool_result_message,
                    strip_recall_from_response,
                    strip_recall_tool,
                )

                recall_result_obj = None
                async for line, result, cap in stream_with_recall_detection(upstream_resp, RECALL_TOOL_NAME):
                    if result is not None and result.is_recall:
                        recall_result_obj = result
                        break
                    if cap is not None:
                        # Stream ended without recall — passthrough completed
                        capture = cap
                        continue
                    if line:
                        yield line + "\n\n"

                if recall_result_obj is not None:
                    # --- Recall interception in streaming ---
                    recall_intercepted = True
                    logger.info(
                        "streaming_recall_tool_call_intercepted",
                        session_id=session_id, turn=turn_number,
                    )
                    _metrics["recall_tool_calls"] = _metrics.get("recall_tool_calls", 0) + 1

                    # Extract the complete tool call from the accumulator
                    tool_calls = recall_result_obj.accumulator.tool_calls
                    recall_tc = None
                    for tc in tool_calls:
                        if tc.get("function", {}).get("name") == RECALL_TOOL_NAME:
                            recall_tc = tc
                            break

                    if not recall_tc:
                        logger.warning("streaming_recall_tool_call_not_found", session_id=session_id)
                        for bl in recall_result_obj.buffered_lines:
                            yield bl + "\n\n"
                        capture = recall_result_obj.capture
                    else:
                        # Parse the question from the tool call
                        func = recall_tc.get("function", {})
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                            question = args.get("question", "")
                        except json.JSONDecodeError:
                            question = ""

                        if not question:
                            logger.warning("streaming_recall_empty_question", session_id=session_id)
                            for bl in recall_result_obj.buffered_lines:
                                yield bl + "\n\n"
                            capture = recall_result_obj.capture
                        else:
                            # Execute the recall query
                            recall_text = await handle_recall_tool_call(
                                http_client=request.app.state.http_client,
                                session_id=session_id,
                                question=question,
                                turn_number=turn_number,
                            )

                            # Assemble the model message from the buffered stream
                            first_response = _assemble_streaming_response(
                                recall_result_obj.capture._chunks,
                                recall_result_obj.accumulator,
                            )
                            model_message = first_response.get("choices", [{}])[0].get("message", {})

                            # Strip the recall tool call from the model message
                            remaining_calls = [
                                tc for tc in model_message.get("tool_calls", [])
                                if tc.get("function", {}).get("name") != RECALL_TOOL_NAME
                            ]

                            # Build the re-send message array
                            resend_messages = list(messages or [])
                            resend_model_msg = dict(model_message)
                            if remaining_calls:
                                resend_model_msg["tool_calls"] = remaining_calls
                            else:
                                resend_model_msg.pop("tool_calls", None)
                            if not remaining_calls and not resend_model_msg.get("content"):
                                resend_model_msg["content"] = None
                            resend_messages.append(resend_model_msg)
                            resend_messages.append(
                                build_tool_result_message(recall_tc.get("id", "recall_0"), recall_text)
                            )

                            # Strip the recall tool from the tools array for the re-send
                            body_dict = json.loads(body)
                            strip_recall_tool(body_dict)

                            # Re-send as non-streaming for reliable interception
                            resend_body = json.dumps({
                                **body_dict,
                                "stream": False,
                                "messages": resend_messages,
                            }).encode("utf-8")

                            try:
                                second_resp = await _upstream_request_with_retry(
                                    client=request.app.state.http_client,
                                    method="POST",
                                    url=url,
                                    headers=headers,
                                    content=resend_body,
                                )
                            except (httpx.TimeoutException, httpx.ConnectError) as e:
                                _metrics["upstream_errors"] += 1
                                error = json.dumps(
                                    {"error": {"message": f"Upstream error during streaming recall re-send: {e}", "type": "upstream_error"}}
                                )
                                yield f"data: {error}\n\n"
                                return

                            if second_resp.status_code >= 400:
                                _metrics["upstream_errors"] += 1
                                error_body = second_resp.text
                                yield f"data: {error_body}\n\n"
                                return

                            # Check if the second response ALSO calls recall
                            second_data = second_resp.json()
                            second_recall_tc = find_recall_tool_call(second_data)

                            if second_recall_tc:
                                # Handle one more recall (max 2 per turn to prevent loops)
                                logger.info("streaming_recall_second_call", session_id=session_id, turn=turn_number)
                                _metrics["recall_tool_calls"] = _metrics.get("recall_tool_calls", 0) + 1

                                second_func = second_recall_tc.get("function", {})
                                try:
                                    second_args = json.loads(second_func.get("arguments", "{}"))
                                    second_question = second_args.get("question", "")
                                except json.JSONDecodeError:
                                    second_question = ""

                                if second_question:
                                    second_recall_text = await handle_recall_tool_call(
                                        http_client=request.app.state.http_client,
                                        session_id=session_id,
                                        question=second_question,
                                        turn_number=turn_number,
                                    )

                                    second_model_msg = second_data["choices"][0]["message"]
                                    second_remaining_calls = [
                                        tc for tc in second_model_msg.get("tool_calls", [])
                                        if tc.get("function", {}).get("name") != RECALL_TOOL_NAME
                                    ]
                                    third_messages = list(resend_messages)
                                    third_model_msg = dict(second_model_msg)
                                    if second_remaining_calls:
                                        third_model_msg["tool_calls"] = second_remaining_calls
                                    else:
                                        third_model_msg.pop("tool_calls", None)
                                    if not second_remaining_calls and not third_model_msg.get("content"):
                                        third_model_msg["content"] = None
                                    third_messages.append(third_model_msg)
                                    third_messages.append(build_tool_result_message(
                                        second_recall_tc.get("id", "recall_1"), second_recall_text,
                                    ))

                                    third_body = json.dumps({
                                        **body_dict,
                                        "stream": False,
                                        "messages": third_messages,
                                    }).encode("utf-8")

                                    try:
                                        third_resp = await _upstream_request_with_retry(
                                            client=request.app.state.http_client,
                                            method="POST",
                                            url=url,
                                            headers=headers,
                                            content=third_body,
                                        )
                                        if third_resp.status_code < 400:
                                            second_data = third_resp.json()
                                        else:
                                            _metrics["upstream_errors"] += 1
                                    except (httpx.TimeoutException, httpx.ConnectError):
                                        _metrics["upstream_errors"] += 1

                            # Strip recall tool from the final response
                            strip_recall_from_response(second_data)

                            # Convert the non-streaming response to SSE format and yield
                            sse_lines = _non_streaming_to_sse(second_data)
                            for sse_line in sse_lines:
                                yield sse_line + "\n\n"

                            # Set up capture from the final response for extraction
                            # The re-send was non-streaming, so use set_non_streaming_response
                            # which handles message.content (not delta.content) correctly.
                            capture = ResponseCapture()
                            capture.set_non_streaming_response(second_data)

            else:
                # Standard passthrough — no recall detection needed
                async for line, cap in stream_with_capture(upstream_resp):
                    if cap is not None:
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
        if session_id and not recall_intercepted:
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
        elif session_id and recall_intercepted and capture and capture.get_full_text():
            # For recall interception, schedule extraction for the final response
            async def post_recall_stream_extraction():
                await _run_extraction(
                    client=request.app.state.extractor_client,
                    session_id=session_id,
                    turn_number=turn_number,
                    messages=messages or [],
                    response_text=capture.get_full_text(),
                    truncated=capture.truncated,
                )
            background_tasks.add_task(post_recall_stream_extraction)

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
    recall_injected: bool = False,
) -> Response:
    """Handle non-streaming request with retry, recall interception, and extraction."""
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

    # Check for recall tool call interception (non-streaming only for now)
    if recall_injected and session_id:
        try:
            from src.proxy.tool_injection import (
                find_recall_tool_call,
                handle_recall_tool_call,
                build_tool_result_message,
                strip_recall_from_response,
                strip_recall_tool,
                RECALL_TOOL_NAME,
            )

            data = resp.json()
            tool_call = find_recall_tool_call(data)

            if tool_call:
                logger.info(
                    "recall_tool_call_intercepted",
                    session_id=session_id,
                    turn=turn_number,
                )
                _metrics["recall_tool_calls"] = _metrics.get("recall_tool_calls", 0) + 1

                # Parse the question from the tool call
                func = tool_call.get("function", {})
                try:
                    args = json.loads(func.get("arguments", "{}"))
                    question = args.get("question", "")
                except json.JSONDecodeError:
                    question = ""

                if question:
                    # Handle the recall: query session graph
                    recall_result = await handle_recall_tool_call(
                        http_client=request.app.state.http_client,
                        session_id=session_id,
                        question=question,
                        turn_number=turn_number,
                    )

                    # Build the tool result message
                    tool_call_id = tool_call.get("id", "recall_0")
                    tool_result_msg = build_tool_result_message(tool_call_id, recall_result)

                    # Reconstruct messages: original messages + model's recall call + tool result
                    original_messages = messages or []
                    model_message = data["choices"][0]["message"]

                    # Strip the recall tool call from the model message
                    remaining_calls = [
                        tc for tc in model_message.get("tool_calls", [])
                        if tc.get("function", {}).get("name") != RECALL_TOOL_NAME
                    ]

                    # Build the re-send message array
                    resend_messages = list(original_messages)
                    resend_model_msg = dict(model_message)
                    if remaining_calls:
                        resend_model_msg["tool_calls"] = remaining_calls
                    else:
                        resend_model_msg.pop("tool_calls", None)
                    # If the model only had the recall tool call, keep the message
                    # but make it clear the model chose to recall
                    if not remaining_calls and not resend_model_msg.get("content"):
                        resend_model_msg["content"] = None
                    resend_messages.append(resend_model_msg)
                    resend_messages.append(tool_result_msg)

                    # Strip the recall tool from the tools array for the re-send
                    body_dict = json.loads(body)
                    strip_recall_tool(body_dict)

                    # Re-send to upstream with the tool result
                    resend_body = json.dumps({
                        **body_dict,
                        "messages": resend_messages,
                    }).encode("utf-8")

                    try:
                        second_resp = await _upstream_request_with_retry(
                            client=request.app.state.http_client,
                            method="POST",
                            url=url,
                            headers=headers,
                            content=resend_body,
                            max_retries=settings.upstream_max_retries,
                            backoff_base=settings.upstream_retry_backoff_base_s,
                        )

                        # Strip recall tool from the final response
                        final_data = second_resp.json()
                        strip_recall_from_response(final_data)

                        # Schedule extraction for the second response
                        final_response_text = ""
                        final_choices = final_data.get("choices", [])
                        if final_choices:
                            final_msg = final_choices[0].get("message", {})
                            final_response_text = final_msg.get("content", "") or ""

                        if session_id and final_response_text:
                            background_tasks.add_task(
                                _run_extraction,
                                client=request.app.state.extractor_client,
                                session_id=session_id,
                                turn_number=turn_number,
                                messages=resend_messages,
                                response_text=final_response_text,
                            )

                        return Response(
                            content=json.dumps(final_data).encode(),
                            status_code=second_resp.status_code,
                            media_type="application/json",
                            background=background_tasks,
                        )
                    except httpx.TimeoutException:
                        _metrics["upstream_errors"] += 1
                        return make_error_response(504, "Upstream request timed out during recall re-send", "upstream_timeout", code="timeout")
                    except httpx.ConnectError as e:
                        _metrics["upstream_errors"] += 1
                        return make_error_response(502, f"Upstream connection failed during recall re-send: {e}", "upstream_error")
                else:
                    # Model called recall with empty question — return original response
                    logger.warning("recall_tool_empty_question", session_id=session_id)
        except Exception as e:
            logger.warning("recall_interception_failed", session_id=session_id, error=str(e), exc_info=True)
            # Fall through to return original response

    # Schedule extraction as background task (original response, no recall)
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

    # Strip recall tool from the final response if it was injected
    if recall_injected:
        try:
            from src.proxy.tool_injection import strip_recall_from_response
            data = resp.json()
            strip_recall_from_response(data)
            return Response(
                content=json.dumps(data).encode(),
                status_code=resp.status_code,
                media_type="application/json",
                background=background_tasks,
            )
        except Exception:
            pass  # Fall back to original response

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
