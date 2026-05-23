""" /v1/chat/completions endpoint — proxy with session resolution, context assembly, and extraction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx
import structlog
from fastapi import APIRouter, Request
from starlette.background import BackgroundTasks
from starlette.responses import Response, StreamingResponse

from archolith_proxy.assembler.context import assemble_context
from archolith_proxy.config import get_settings
from archolith_proxy.extractor.client import extract_facts
from archolith_proxy.graph.backend import get_backend, is_graph_ready
from archolith_proxy.metrics import get_metrics, record_assembly_mode, record_metric
from archolith_proxy.models.graph_nodes import FactType, FileStatus
from archolith_proxy.openai.errors import make_error_response
from archolith_proxy.openai.schemas import ChatCompletionRequest
from archolith_proxy.proxy.rewrite import estimate_input_tokens, rewrite_messages, strip_reasoning
from archolith_proxy.proxy.session import resolve_session
from archolith_proxy.proxy.live import (
    broadcast_request, broadcast_assembly, broadcast_response,
    broadcast_extraction, broadcast_session_event, broadcast_recall,
)
from archolith_proxy.proxy.streaming import ResponseCapture, stream_with_capture, stream_with_recall_detection, _assemble_streaming_response, _non_streaming_to_sse
from archolith_proxy.proxy.upstream import RETRYABLE_STATUS_CODES, upstream_request_with_retry
from archolith_proxy.rtk import filter_request_body
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()

router = APIRouter()


def _normalize_message_content(content: object) -> str:
    """Flatten OpenAI-style message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = _normalize_message_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if nested is not None:
            return _normalize_message_content(nested)
    return ""


def _extract_response_text(response_data: dict) -> str:
    """Extract assistant text from a non-streaming chat completion response."""
    choices = response_data.get("choices", [])
    if not choices:
        return ""
    message = choices[0].get("message", {})
    return _normalize_message_content(message.get("content"))


def _extract_file_reads(messages: list[dict]) -> list[dict]:
    """Pair file-read tool calls with their results via tool_call_id.

    Iterates the messages array to build a lookup from assistant tool_calls,
    then matches tool result messages by tool_call_id. Only returns pairs
    where the tool is NOT a compressible tool (i.e., it's a file-read tool)
    and content is a non-empty string.

    Returns list of {path, content, tool_call_id, tool_name}.
    """
    from archolith_proxy.proxy.rewrite import _is_compressible_tool

    # Build lookup: tool_call_id → (name, parsed_args)
    call_map: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or []):
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (KeyError, json.JSONDecodeError):
                    args = {}
                call_map[tc["id"]] = (tc["function"]["name"], args)

    # Match tool results to calls
    results = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tc_id = msg.get("tool_call_id", "")
        if tc_id not in call_map:
            continue
        name, args = call_map[tc_id]
        if _is_compressible_tool(name):
            continue  # search/grep/web — not file content
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        path = args.get("path") or args.get("file_path") or args.get("filename") or ""
        if not path:
            continue
        results.append({
            "path": path, "content": content,
            "tool_call_id": tc_id, "tool_name": name,
        })
    return results


def _collect_recent_tool_results(messages: list[dict], max_chars: int = 4000) -> str | None:
    """Serialize the newest tool results first within the extraction budget."""
    recent_entries: list[str] = []
    used = 0

    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue

        content = _normalize_message_content(msg.get("content")).strip()
        if not content:
            continue

        tool_name = msg.get("name", "unknown_tool")
        entry = f"Tool [{tool_name}]:\n{content}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(entry) > remaining:
            entry = entry[:remaining]
        recent_entries.append(entry)
        used += len(entry)

    if not recent_entries:
        return None

    recent_entries.reverse()
    return "\n\n".join(recent_entries)



@router.post("/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Accept OpenAI chat completion requests, forward to upstream,
    resolve session, assemble context, and trigger async fact extraction."""
    settings = get_settings()

    # Start trace builder for observability
    trace_builder = TraceBuilder()

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
    neo4j_ready = is_graph_ready()

    # Guard: if lifespan didn't initialize http_client, return 503
    if not hasattr(request.app.state, "http_client"):
        return make_error_response(503, "Proxy not initialized — lifespan did not complete", "server_error")

    if neo4j_ready:
        try:
            headers = {k: v for k, v in request.headers.items()}
            messages_raw = body.get("messages", [])
            session_id, is_new = await resolve_session(headers, messages_raw)
            turn_number = await get_backend().get_turn_number(session_id)
            logger.debug("session_resolved", session_id=session_id, turn=turn_number, is_new=is_new)

            # Set initial session goal from first user message on new sessions
            if is_new:
                first_user_msg = ""
                for msg in messages_raw:
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                p.get("text", "") for p in content if isinstance(p, dict)
                            )
                        first_user_msg = content[:200]
                        break
                if first_user_msg:
                    # Truncate to a single-sentence goal
                    goal = first_user_msg.split("\n")[0].strip()[:120]
                    try:
                        await get_backend().update_goal(session_id, goal)
                        logger.info("session_goal_set_initial", session_id=session_id, goal=goal[:80])
                        await broadcast_session_event(session_id, "session_created", goal=goal)
                    except Exception as e:
                        logger.warning("session_goal_set_failed", session_id=session_id, error=str(e))

            # Bind session context for request-level logging middleware
            structlog.contextvars.bind_contextvars(
                session_id=session_id,
                turn_number=turn_number,
            )
        except Exception as e:
            logger.warning("session_resolution_failed", error=str(e))
            record_metric("neo4j_errors", 1)

    # Fetch current session goal for extraction context
    session_goal = None
    if session_id and neo4j_ready:
        try:
            session_data = await get_backend().find_session_by_id(session_id)
            session_goal = session_data.get("goal") if session_data else None
        except Exception:
            pass  # Non-critical — extraction will proceed without goal context

    # Context assembly — rewrite messages if graph has enough data
    assembled = None
    assembly_mode = "passthrough"
    assembly_latency_ms = 0.0
    rewritten_tokens = 0
    savings = 0
    savings_ratio = 0.0
    messages = body.get("messages", [])
    input_tokens = estimate_input_tokens(messages)
    record_metric("total_input_tokens_seen", input_tokens)
    user_turn_count = sum(1 for m in messages if m.get("role") == "user")

    # Trace: record request arrival
    trace_builder.set_request(
        session_id=session_id,
        turn_number=turn_number,
        model=req.model,
        stream=req.stream,
        input_tokens=input_tokens,
        message_count=len(messages),
        user_turn_count=user_turn_count,
    )
    trace_builder.set_original_messages(body.get("messages", []))

    # Live stream: broadcast incoming request
    await broadcast_request(
        session_id=session_id, turn_number=turn_number,
        model=req.model, message_count=len(body.get("messages", [])),
        stream=req.stream, input_tokens=input_tokens,
    )

    if session_id and neo4j_ready:
        try:
            # Wait for prior turn's extraction to commit before reading graph state
            from archolith_proxy.proxy.locks import wait_for_prior_extraction
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

            # Try curator first (LLM-driven context manager)
            if settings.curator_enabled:
                try:
                    from archolith_proxy.curator import curate_context
                    record_metric("curator_calls", 1)
                    assembled = await curate_context(
                        session_id=session_id,
                        turn_number=turn_number,
                        user_message=user_message or "",
                        session_goal=session_goal,
                        http_client=request.app.state.http_client,
                        messages=body.get("messages", []),
                    )
                    if assembled:
                        assembly_mode = "curator"
                except Exception:
                    logger.warning("curator_error", session_id=session_id, exc_info=True)

            # Fall back to heuristic assembler if curator didn't produce a result
            if assembled is None:
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
                body["messages"] = rewrite_messages(
                    body.get("messages", []),
                    assembled,
                    settings.coherence_tail_size,
                    max_tail_messages=settings.max_tail_messages,
                )
                rewritten_count = len(body["messages"])
                assembly_mode = "graph"

                # Estimate token savings
                rewritten_tokens = estimate_input_tokens(body["messages"])
                savings = max(0, input_tokens - rewritten_tokens)

                # Savings-ratio gate: revert to passthrough if rewriting
                # doesn't save meaningful tokens. Short/moderate conversations
                # lose more continuity than they gain in compression.
                savings_ratio = savings / max(input_tokens, 1)
                if input_tokens < settings.assembly_min_input_tokens:
                    # Conversation is small enough to fit entirely — passthrough
                    logger.info(
                        "assembly_skipped_low_tokens",
                        session_id=session_id,
                        turn=turn_number,
                        input_tokens=input_tokens,
                        min_tokens=settings.assembly_min_input_tokens,
                        savings_ratio=round(savings_ratio, 3),
                    )
                    body["messages"] = original_messages
                    rewritten_count = original_count
                    rewritten_tokens = input_tokens
                    savings = 0
                    savings_ratio = 0.0
                    assembly_mode = "skipped_low_tokens"
                elif savings_ratio < settings.assembly_min_savings_ratio:
                    # Rewriting barely saves anything — keep full history
                    logger.info(
                        "assembly_skipped_low_savings",
                        session_id=session_id,
                        turn=turn_number,
                        savings_ratio=round(savings_ratio, 3),
                        min_ratio=settings.assembly_min_savings_ratio,
                        savings_tokens=savings,
                        input_tokens=input_tokens,
                    )
                    body["messages"] = original_messages
                    rewritten_count = original_count
                    rewritten_tokens = input_tokens
                    savings = 0
                    savings_ratio = 0.0
                    assembly_mode = "skipped_low_savings"
                else:
                    # Savings justify rewriting — proceed with compaction check

                    # Context-overflow compaction: if rewritten payload still exceeds
                    # budget, try LLM compaction of the graph context block
                    if (
                        settings.compaction_enabled
                        and rewritten_tokens > settings.context_token_budget
                        and assembled
                    ):
                        try:
                            from archolith_proxy.assembler.compaction import compact_context

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
                                if assembled.graph_context:
                                    assembled.graph_context[0] = {
                                        **assembled.graph_context[0],
                                        "content": compacted,
                                    }
                                else:
                                    assembled.graph_context = [{"role": "system", "content": compacted}]
                                # Re-rewrite with compacted context
                                body["messages"] = rewrite_messages(
                                    original_messages,
                                    assembled,
                                    settings.coherence_tail_size,
                                    max_tail_messages=settings.max_tail_messages,
                                )
                                rewritten_count = len(body["messages"])
                                rewritten_tokens = estimate_input_tokens(body["messages"])
                                savings = max(0, input_tokens - rewritten_tokens)
                                savings_ratio = savings / max(input_tokens, 1)
                                logger.info(
                                    "context_compaction_applied",
                                    session_id=session_id,
                                    turn=turn_number,
                                    rewritten_tokens=rewritten_tokens,
                                    budget=settings.context_token_budget,
                                )
                                record_metric("compaction_applied", 1)
                        except Exception as e:
                            logger.warning(
                                "context_compaction_failed",
                                session_id=session_id,
                                turn=turn_number,
                                error=str(e),
                            )
                            # Compaction failed — keep the oversized assembled context
                            # (better than passthrough, which would send full history)

                    record_metric("token_savings_estimated", savings)
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

                # Trace: capture the actual outbound messages after savings gates.
                trace_builder.set_rewritten_messages(body.get("messages", []))
            else:
                # assemble_context returned None = cold start
                assembly_mode = "cold_start"
        except Exception as e:
            logger.warning("context_assembly_failed", session_id=session_id, error=str(e), exc_info=True)
            assembly_mode = "fallback"
            trace_builder.set_fallback_reason(str(e)[:200])
            record_metric("neo4j_errors", 1)
            # Fall through to passthrough — assembly failure must not block requests

    # Trace: record assembly result
    trace_builder.set_assembly(
        mode=assembly_mode,
        reason="session not ready" if not (session_id and neo4j_ready) else "",
        latency_ms=assembly_latency_ms,
        facts_selected=[{"content": m.get("content", "")[:100]} for m in (assembled.graph_context if assembled else [])],
        files_selected=assembled.files_selected if assembled else [],
        decisions_selected=assembled.decisions_selected if assembled else [],
        rewritten_tokens=rewritten_tokens,
        savings_tokens=savings,
        savings_ratio=savings_ratio,
        compression_ratio=assembled.compression_ratio if assembled else 1.0,
    )

    record_assembly_mode(assembly_mode)

    # Live stream: broadcast assembly result
    await broadcast_assembly(
        session_id=session_id, turn_number=turn_number,
        mode=assembly_mode,
        facts_injected=assembled.facts_retrieved if assembled else 0,
        token_savings=savings if assembled else 0,
        latency_ms=assembly_latency_ms if assembled else 0.0,
    )

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
        from archolith_proxy.proxy.tool_injection import inject_recall_tool
        body = inject_recall_tool(body)
        recall_injected = True

    body = filter_request_body(body, enabled=settings.rtk_enabled)
    request_body = json.dumps(body).encode("utf-8")

    if req.stream:
        return await _handle_streaming(
            request, background_tasks, upstream_url, upstream_headers, request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []), recall_injected=recall_injected,
            session_goal=session_goal,
            trace_builder=trace_builder,
        )
    else:
        return await _handle_non_streaming(
            request, background_tasks, upstream_url, upstream_headers, request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []), recall_injected=recall_injected,
            session_goal=session_goal,
            trace_builder=trace_builder,
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
    session_goal: str | None = None,
    trace_builder: TraceBuilder | None = None,
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
       model calls __archolith_recall in the stream, intercept,
       execute recall, re-send non-streaming, then convert the second
       response to SSE format and relay to client.

    Limitation: once SSE chunks start flowing to the client (phase 2),
    retry is impossible — the client has already consumed partial output.
    Mid-stream errors result in a broken stream.
    """
    settings = get_settings()
    capture_holder = {"capture": None, "recall_intercepted": False}

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
                if resp.status_code in RETRYABLE_STATUS_CODES:
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
                        record_metric("upstream_errors", 1)
                        error = json.dumps(
                            {"error": {"message": f"Upstream returned {resp.status_code} after {max_retries} retries", "type": "upstream_error"}}
                        )
                        yield f"data: {error}\n\n"
                        return

                # Non-retryable error (e.g. 400, 401) — relay and stop
                if resp.status_code >= 400:
                    record_metric("upstream_errors", 1)
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

                # Trace: record upstream response (streaming)
                if trace_builder:
                    trace_builder.set_response(
                        status=resp.status_code,
                        latency_ms=0.0,
                        output_tokens=None,
                        response_summary="",
                    )
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
                    record_metric("upstream_errors", 1)
                    error = json.dumps(
                        {"error": {"message": f"Upstream connection failed after {max_retries} retries: {e}", "type": "upstream_error"}}
                    )
                    yield f"data: {error}\n\n"
                    return
        else:
            record_metric("upstream_errors", 1)
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
                from archolith_proxy.proxy.tool_injection import (
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
                    get_metrics()["recall_tool_calls"] = get_metrics().get("recall_tool_calls", 0) + 1

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

                            # Live stream: broadcast recall event
                            await broadcast_recall(
                                session_id=session_id, turn_number=turn_number,
                                question=question, facts_returned=0,
                            )
                            if trace_builder:
                                trace_builder.set_recall(used=True, question=question, facts_returned=0)

                            # Assemble the model message from the buffered stream
                            first_response = _assemble_streaming_response(
                                recall_result_obj.capture._chunks,
                                recall_result_obj.accumulator,
                            )
                            model_message = first_response.get("choices", [{}])[0].get("message", {})

                            # Build the re-send message array — keep the recall
                            # tool_call in the assistant message so the tool result
                            # has a matching tool_call_id (OpenAI requires this).
                            resend_messages = list(messages or [])
                            resend_model_msg = dict(model_message)
                            resend_messages.append(resend_model_msg)
                            resend_messages.append(
                                build_tool_result_message(recall_tc.get("id", "recall_0"), recall_text)
                            )

                            # Strip the recall tool from the tools array for the re-send
                            body_dict = json.loads(body)
                            strip_recall_tool(body_dict)

                            # Re-send as non-streaming for reliable interception
                            resend_payload = filter_request_body({
                                **body_dict,
                                "stream": False,
                                "messages": resend_messages,
                            }, enabled=settings.rtk_enabled)
                            resend_body = json.dumps(resend_payload).encode("utf-8")

                            try:
                                second_resp = await upstream_request_with_retry(
                                    client=request.app.state.http_client,
                                    url=url,
                                    headers=headers,
                                    content=resend_body,
                                )
                            except (httpx.TimeoutException, httpx.ConnectError) as e:
                                record_metric("upstream_errors", 1)
                                error = json.dumps(
                                    {"error": {"message": f"Upstream error during streaming recall re-send: {e}", "type": "upstream_error"}}
                                )
                                yield f"data: {error}\n\n"
                                return

                            if second_resp.status_code >= 400:
                                record_metric("upstream_errors", 1)
                                error_body = second_resp.text
                                yield f"data: {error_body}\n\n"
                                return

                            # Check if the second response ALSO calls recall
                            second_data = second_resp.json()
                            second_recall_tc = find_recall_tool_call(second_data)

                            if second_recall_tc:
                                # Handle one more recall (max 2 per turn to prevent loops)
                                logger.info("streaming_recall_second_call", session_id=session_id, turn=turn_number)
                                get_metrics()["recall_tool_calls"] = get_metrics().get("recall_tool_calls", 0) + 1

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

                                    # Live stream: broadcast second recall event
                                    await broadcast_recall(
                                        session_id=session_id, turn_number=turn_number,
                                        question=second_question, facts_returned=0,
                                    )
                                    if trace_builder:
                                        trace_builder.set_recall(used=True, question=question, facts_returned=0)

                                    # Keep assistant tool_calls intact for the
                                    # same reason as the first round.
                                    second_model_msg = second_data["choices"][0]["message"]
                                    third_messages = list(resend_messages)
                                    third_model_msg = dict(second_model_msg)
                                    third_messages.append(third_model_msg)
                                    third_messages.append(build_tool_result_message(
                                        second_recall_tc.get("id", "recall_1"), second_recall_text,
                                    ))

                                    third_payload = filter_request_body({
                                        **body_dict,
                                        "stream": False,
                                        "messages": third_messages,
                                    }, enabled=settings.rtk_enabled)
                                    third_body = json.dumps(third_payload).encode("utf-8")

                                    try:
                                        third_resp = await upstream_request_with_retry(
                                            client=request.app.state.http_client,
                                                    url=url,
                                            headers=headers,
                                            content=third_body,
                                        )
                                        if third_resp.status_code < 400:
                                            second_data = third_resp.json()
                                        else:
                                            record_metric("upstream_errors", 1)
                                    except (httpx.TimeoutException, httpx.ConnectError):
                                        record_metric("upstream_errors", 1)

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
            record_metric("upstream_errors", 1)
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
        capture_holder["recall_intercepted"] = recall_intercepted

    # Live stream: broadcast response event (streaming — runs after stream completes)
    async def _broadcast_streaming_response():
        await broadcast_response(
            session_id=session_id, turn_number=turn_number,
            status=200, latency_ms=0.0, output_tokens=None,
        )
    background_tasks.add_task(_broadcast_streaming_response)

    async def _finalize_streaming_trace_and_extraction():
        cap = capture_holder.get("capture")
        response_text = cap.get_full_text() if cap else ""

        if trace_builder:
            trace_builder.set_response(
                status=200,
                latency_ms=0.0,
                output_tokens=None,
                response_summary=response_text,
            )

        if session_id and response_text:
            try:
                await _run_extraction(
                    client=request.app.state.extractor_client,
                    session_id=session_id,
                    turn_number=turn_number,
                    messages=messages or [],
                    response_text=response_text,
                    truncated=cap.truncated if cap else False,
                    session_goal=session_goal,
                    trace_builder=trace_builder,
                    promotion_service=getattr(request.app.state, "promotion_service", None),
                )
            except Exception:
                logger.warning("streaming_extraction_finalize_failed", exc_info=True)

        if trace_builder:
            try:
                trace = trace_builder.build()
                await get_trace_store().record(trace)
            except Exception:
                logger.warning("trace_store_failed_streaming", exc_info=True)

    background_tasks.add_task(_finalize_streaming_trace_and_extraction)

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
    session_goal: str | None = None,
    trace_builder: TraceBuilder | None = None,
) -> Response:
    """Handle non-streaming request with retry, recall interception, and extraction."""
    settings = get_settings()

    try:
        resp = await upstream_request_with_retry(
            client=request.app.state.http_client,
            url=url,
            headers=headers,
            content=body,
            max_retries=settings.upstream_max_retries,
            backoff_base=settings.upstream_retry_backoff_base_s,
        )
    except httpx.TimeoutException:
        record_metric("upstream_errors", 1)
        return make_error_response(504, "Upstream request timed out", "upstream_timeout", code="timeout")
    except httpx.ConnectError as e:
        record_metric("upstream_errors", 1)
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    # Check for recall tool call interception via the shared helper.
    # This handles up to 2 recall rounds consistently with the streaming path.
    recall_result = None
    final_data = None
    response_text = ""
    if recall_injected and session_id:
        try:
            from archolith_proxy.proxy.recall import handle_non_streaming_recall

            recall_result = await handle_non_streaming_recall(
                resp=resp,
                http_client=request.app.state.http_client,
                url=url,
                headers=headers,
                body=body,
                session_id=session_id,
                turn_number=turn_number,
                original_messages=messages or [],
                max_retries=settings.upstream_max_retries,
                backoff_base=settings.upstream_retry_backoff_base_s,
            )

            if recall_result.recall_used and recall_result.final_data is not None:
                final_data = recall_result.final_data

                # Broadcast recall events for each round
                for q, fc in zip(recall_result.recall_questions, recall_result.facts_returned_counts):
                    await broadcast_recall(
                        session_id=session_id, turn_number=turn_number,
                        question=q, facts_returned=fc,
                    )

                # Set trace recall info (use the first question for the trace)
                if trace_builder and recall_result.recall_questions:
                    trace_builder.set_recall(
                        used=True,
                        question=recall_result.recall_questions[0],
                        facts_returned=recall_result.facts_returned_counts[0] if recall_result.facts_returned_counts else 0,
                    )

                # Trace: update response to reflect the actual final response
                if trace_builder:
                    trace_builder.set_response(
                        status=resp.status_code,
                        latency_ms=0.0,
                        output_tokens=None,
                        response_summary=_extract_response_text(final_data),
                    )

                # Schedule extraction for the final response
                response_text = _extract_response_text(final_data)

                if session_id and response_text:
                    # Determine the messages that led to the final response
                    # (the recall re-send added tool result messages)
                    extraction_messages = messages or []
                    background_tasks.add_task(
                        _run_extraction,
                        client=request.app.state.extractor_client,
                        session_id=session_id,
                        turn_number=turn_number,
                        messages=extraction_messages,
                        response_text=response_text,
                        session_goal=session_goal,
                        trace_builder=trace_builder,
                        promotion_service=getattr(request.app.state, "promotion_service", None),
                    )
        except Exception as e:
            logger.warning("recall_interception_failed", session_id=session_id, error=str(e), exc_info=True)
            # Fall through to use original response

    # Live stream: broadcast response event (non-streaming — always, for both recall and normal)
    await broadcast_response(
        session_id=session_id, turn_number=turn_number,
        status=resp.status_code, latency_ms=0.0, output_tokens=None,
    )

    # Schedule extraction for the original response if recall was NOT used
    if session_id and not (recall_result and recall_result.recall_used and final_data is not None):
        try:
            data = resp.json()
            response_text = _extract_response_text(data)

            if response_text:
                background_tasks.add_task(
                    _run_extraction,
                    client=request.app.state.extractor_client,
                    session_id=session_id,
                    turn_number=turn_number,
                    messages=messages or [],
                    response_text=response_text,
                    session_goal=session_goal,
                    trace_builder=trace_builder,
                    promotion_service=getattr(request.app.state, "promotion_service", None),
                )
        except Exception as e:
            logger.warning("non_streaming_extraction_setup_failed", error=str(e))

    # Trace: record upstream response (non-streaming) — always stored
    if trace_builder:
        if not (recall_result and recall_result.recall_used and final_data is not None):
            # Normal path: trace the original response
            trace_builder.set_response(
                status=resp.status_code,
                latency_ms=0.0,
                output_tokens=None,
                response_summary=response_text,
            )

    # Build and return the response
    if final_data is not None:
        # Recall path: return the final data from the recall interception
        # Strip recall tool from the final response if still present
        try:
            from archolith_proxy.proxy.tool_injection import strip_recall_from_response
            strip_recall_from_response(final_data)
        except Exception:
            pass

        # Store trace as background task (recall path)
        if trace_builder:
            async def _store_trace_recall():
                try:
                    trace = trace_builder.build()
                    await get_trace_store().record(trace)
                except Exception:
                    logger.warning("trace_store_failed_recall", exc_info=True)
            background_tasks.add_task(_store_trace_recall)

        return Response(
            content=json.dumps(final_data).encode(),
            status_code=resp.status_code,
            media_type="application/json",
            background=background_tasks,
        )

    # Normal path (no recall)
    # Strip recall tool from the final response if it was injected
    if recall_injected:
        try:
            from archolith_proxy.proxy.tool_injection import strip_recall_from_response
            data = resp.json()
            strip_recall_from_response(data)

            # Trace: store turn trace as background task (normal path)
            if trace_builder:
                async def _store_trace_non_stream():
                    try:
                        trace = trace_builder.build()
                        await get_trace_store().record(trace)
                    except Exception:
                        logger.warning("trace_store_failed_non_stream", exc_info=True)
                background_tasks.add_task(_store_trace_non_stream)

            return Response(
                content=json.dumps(data).encode(),
                status_code=resp.status_code,
                media_type="application/json",
                background=background_tasks,
            )
        except Exception:
            pass  # Fall back to original response

    # Trace: store turn trace as background task (fallback passthrough)
    if trace_builder:
        async def _store_trace_non_stream():
            try:
                trace = trace_builder.build()
                await get_trace_store().record(trace)
            except Exception:
                logger.warning("trace_store_failed_non_stream", exc_info=True)
        background_tasks.add_task(_store_trace_non_stream)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type="application/json",
        background=background_tasks,
    )


async def _upsert_file_cache(session_id: str, file_reads: list[dict], turn: int) -> None:
    """Store file-read content into the graph backend's file content cache.

    Skips files exceeding the configured max byte size. Uses sha256
    deduplication — if the file's content hasn't changed, no DB write
    is needed (common case on re-reads of unmodified files).
    """
    settings = get_settings()
    backend = get_backend()
    for fr in file_reads:
        content = fr["content"]
        if len(content.encode()) > settings.file_cache_max_file_bytes:
            logger.debug("file_cache_skipped_too_large", path=fr["path"], session_id=session_id)
            continue
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        try:
            await backend.upsert_file_content(
                session_id=session_id, path=fr["path"],
                content=content, sha256=sha256, turn=turn,
            )
        except Exception:
            logger.warning("file_cache_upsert_failed", path=fr["path"], session_id=session_id, exc_info=True)


async def _run_extraction(
    client,
    session_id: str,
    turn_number: int,
    messages: list[dict],
    response_text: str,
    truncated: bool = False,
    session_goal: str | None = None,
    trace_builder: TraceBuilder | None = None,
    promotion_service: object | None = None,
) -> None:
    """Run fact extraction and store results in graph. Best-effort, non-blocking.

    Holds a per-session lock during graph writes so that subsequent assembly
    reads see committed state. After extraction, computes batch embeddings
    for all facts and stores them with their vectors. If embedding fails,
    facts are stored without embeddings (assembler falls back to recency-only
    retrieval).

    When promotion_service is provided and settings.promotion_enabled is True,
    eligible facts are promoted to the configured durable memory engine after
    successful storage.
    """
    from archolith_proxy.proxy.locks import get_session_lock

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
                user_message = _normalize_message_content(msg.get("content"))
                break

        response_text = _normalize_message_content(response_text)
        if not user_message and not response_text:
            return

        # Strip reasoning blocks from the response before extraction
        response_text = strip_reasoning(response_text)

        # Capture file reads before flattening — pairs tool calls with results
        # using tool_call_id so the content cache gets structured file content.
        fc_settings = get_settings()
        if fc_settings.file_cache_enabled:
            try:
                file_reads = _extract_file_reads(messages)
                if file_reads:
                    await _upsert_file_cache(session_id, file_reads, turn_number)
            except Exception:
                logger.warning("file_cache_capture_failed", session_id=session_id, turn=turn_number, exc_info=True)

        # Serialize the newest tool results first so the current turn survives truncation.
        tool_results = _collect_recent_tool_results(messages, max_chars=4000)

        extraction_start = time.monotonic()
        result = await extract_facts(
            http_client=client,
            turn_number=turn_number,
            user_message=user_message[:4000],
            assistant_response=response_text[:8000],
            tool_results=tool_results,
            session_goal=session_goal,
        )
        extraction_latency_ms = (time.monotonic() - extraction_start) * 1000

        # Update session goal if extractor provided one
        if result and result.session_goal:
            try:
                await get_backend().update_goal(session_id, result.session_goal)
                logger.info("session_goal_updated", session_id=session_id, goal=result.session_goal[:80])
                await broadcast_session_event(session_id, "goal_updated", goal=result.session_goal)
            except Exception as e:
                logger.warning("session_goal_update_failed", session_id=session_id, error=str(e))

        if result is None:
            record_metric("extraction_failures", 1)
            logger.warning("extraction_result_missing", session_id=session_id, turn=turn_number)
            if trace_builder:
                trace_builder.set_extraction(extraction_latency_ms=extraction_latency_ms)
            return

        if not result.facts:
            logger.info("extraction_empty", session_id=session_id, turn=turn_number)
            record_metric("extraction_empties", 1)
            if trace_builder:
                trace_builder.set_extraction(extraction_latency_ms=extraction_latency_ms)
            return

        # Deduplicate: fetch existing active facts and filter duplicates
        from archolith_proxy.extractor.dedup import deduplicate_facts as _deduplicate_facts
        existing_facts = await get_backend().get_active_facts(session_id, limit=200)
        unique_facts = _deduplicate_facts(result.facts, existing_facts)
        if len(unique_facts) < len(result.facts):
            logger.info(
                "extraction_dedup_applied",
                session_id=session_id,
                turn=turn_number,
                original=len(result.facts),
                after_dedup=len(unique_facts),
                duplicates_removed=len(result.facts) - len(unique_facts),
            )

        # Batch compute embeddings for deduplicated facts only
        fact_contents = [fact.get("content", "") for fact in unique_facts]
        embeddings = await _compute_fact_embeddings(client, fact_contents)

        # Store deduplicated facts with their embeddings via batch call
        enriched_facts = []
        for i, fact in enumerate(unique_facts):
            fact_type_str = fact.get("fact_type", "observation")
            try:
                fact_type = FactType(fact_type_str)
            except ValueError:
                fact_type = FactType.OBSERVATION
            enriched_facts.append({
                "content": fact.get("content", ""),
                "fact_type": fact_type.value,
                "confidence": fact.get("confidence", 0.5),
                "embedding": embeddings[i] if i < len(embeddings) else None,
            })
        # Capture new fact IDs for SUPERSEDES edge creation after invalidation
        new_fact_ids = await get_backend().store_facts_batch(
            session_id=session_id,
            facts=enriched_facts,
            source_turn=turn_number,
        )

        # Store file touches
        for file_path in result.files_touched:
            status = FileStatus.MODIFIED  # Default — extraction doesn't always distinguish
            await get_backend().create_touches(session_id, file_path, status, turn_number)

        # Store decisions
        for decision in result.decisions:
            await get_backend().store_decision(
                session_id=session_id,
                summary=decision.get("summary", ""),
                rationale=decision.get("rationale"),
                turn=turn_number,
            )

        # Invalidate superseded facts — match description strings to actual fact IDs
        invalidations_matched_count = 0
        if result.invalidated_fact_ids:
            matched_ids = await get_backend().find_matching_fact_ids(
                session_id, result.invalidated_fact_ids
            )
            invalidations_matched_count = len(matched_ids)
            if matched_ids:
                count = await get_backend().invalidate_facts(matched_ids)
                if count:
                    logger.info(
                        "facts_invalidated",
                        count=count,
                        session_id=session_id,
                        turn=turn_number,
                        descriptions=len(result.invalidated_fact_ids),
                        matched_ids=len(matched_ids),
                    )

                    # Create SUPERSEDES edges from each new fact to each
                    # invalidated fact so /trace/graph/{sid}/invalidations
                    # has real chain data for the explorer.
                    for new_fid in new_fact_ids:
                        for old_fid in matched_ids:
                            try:
                                await get_backend().create_supersedes(old_fid, new_fid)
                            except Exception as e:
                                logger.warning(
                                    "supersedes_edge_failed",
                                    old_id=old_fid, new_id=new_fid, error=str(e),
                                )
                    logger.debug(
                        "supersedes_edges_created",
                        session_id=session_id,
                        new_facts=len(new_fact_ids),
                        invalidated=len(matched_ids),
                        edges=len(new_fact_ids) * len(matched_ids),
                    )

        # Log active fact count for monitoring
        active_count = await get_backend().get_active_fact_count(session_id)
        embedding_count = sum(1 for e in embeddings if e is not None)
        record_metric("extraction_successes", 1)
        logger.info(
            "extraction_stored",
            session_id=session_id,
            turn=turn_number,
            facts_stored=len(unique_facts),
            embeddings_computed=embedding_count,
            active_fact_count=active_count,
            extraction_latency_ms=round(extraction_latency_ms, 1),
            warning="high_active_count" if active_count > 200 else None,
        )

        # Live stream: broadcast extraction result
        await broadcast_extraction(
            session_id=session_id, turn_number=turn_number,
            facts_stored=len(unique_facts),
            session_goal=result.session_goal,
            latency_ms=extraction_latency_ms,
        )

        if trace_builder:
            duplicates_skipped = len(result.facts) - len(unique_facts)
            trace_builder.set_extraction(
                facts_stored=len(unique_facts),
                duplicates_skipped=duplicates_skipped,
                invalidations_attempted=len(result.invalidated_fact_ids) if result.invalidated_fact_ids else 0,
                invalidations_matched=invalidations_matched_count,
                extraction_latency_ms=extraction_latency_ms,
                extracted_facts=[{"content": f.get("content", "")[:200], "type": f.get("fact_type", "observation")} for f in unique_facts],
            )

        # --- Promotion: push eligible facts to durable memory engine ---
        if promotion_service is not None:
            try:
                from archolith_proxy.memory.models import PromotionRecord
                from archolith_proxy.memory.promotion import PromotionService

                svc = promotion_service  # type: PromotionService
                settings = get_settings()

                if settings.promotion_enabled:
                    eligible_records = []
                    for fact in unique_facts:
                        fact_type = fact.get("fact_type", "observation")
                        confidence = fact.get("confidence", 0.5)
                        if svc.should_promote(
                            fact_type=fact_type,
                            confidence=confidence,
                            turn_count=turn_number,
                            tags=fact.get("tags", []),
                        ):
                            record = PromotionRecord(
                                session_id=session_id,
                                source_turn=turn_number,
                                fact_type=fact_type,
                                content=fact.get("content", ""),
                                confidence=confidence,
                                session_goal=session_goal,
                                touched_files=result.files_touched if hasattr(result, "files_touched") else [],
                                promotion_reason="auto_extracted",
                                tags=fact.get("tags", []),
                            )
                            eligible_records.append(record)

                    if eligible_records:
                        promo_results = await svc.promote_batch(
                            eligible_records,
                            dry_run=settings.promotion_dry_run,
                        )
                        succeeded = sum(1 for r in promo_results if r.outcome.value == "success")
                        skipped = sum(1 for r in promo_results if r.outcome.value == "skipped")
                        failed = sum(1 for r in promo_results if r.outcome.value == "failed")
                        logger.info(
                            "promotion_completed",
                            session_id=session_id,
                            turn=turn_number,
                            eligible=len(eligible_records),
                            succeeded=succeeded,
                            skipped=skipped,
                            failed=failed,
                        )
            except Exception as e:
                logger.warning("promotion_task_failed", session_id=session_id, turn=turn_number, error=str(e), exc_info=True)

    except Exception as e:
        record_metric("extraction_failures", 1)
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
        from archolith_proxy.extractor.embeddings import compute_embeddings_batch
        return await compute_embeddings_batch(client, texts)
    except Exception as e:
        logger.warning("embedding_computation_failed", error=str(e))
        return [None] * len(texts)
