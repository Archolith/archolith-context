"""Streaming response handler — relays SSE from upstream with recall interception and extraction dispatch."""

from __future__ import annotations

import asyncio
import json

import httpx
import structlog
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTasks

from archolith_proxy.config import get_settings
from archolith_proxy.metrics import get_metrics, record_metric
from archolith_proxy.openai.extraction import _run_extraction
from archolith_proxy.proxy.live import broadcast_recall, broadcast_response
from archolith_proxy.proxy.streaming import (
    ResponseCapture,
    stream_with_capture,
    stream_with_recall_detection,
    _assemble_streaming_response,
    yield_as_sse,
)
from archolith_proxy.proxy.upstream import RETRYABLE_STATUS_CODES, upstream_request_with_retry
from archolith_proxy.filter_adapter import filter_request_body
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()


def _summarize_tool_calls(tool_calls: list[dict]) -> str:
    """Render captured final-message tool_calls as a compact text summary.

    Used (D8) when a re-sent response is a tool-call-only turn with empty
    content, so extraction still receives a meaningful final-message
    representation instead of being skipped on empty text.
    """
    parts: list[str] = []
    for tc in tool_calls or []:
        func = (tc or {}).get("function", {}) or {}
        name = func.get("name") or "unknown"
        args = func.get("arguments") or ""
        if isinstance(args, (dict, list)):
            args = json.dumps(args)
        parts.append(f"[tool_call] {name}({args})")
    return "\n".join(parts)


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
    synthetic_injected: bool = False,
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
                _recall_decision_timeout = get_settings().streaming_recall_decision_timeout_s
                async for line, result, cap in stream_with_recall_detection(
                    upstream_resp, RECALL_TOOL_NAME, decision_timeout_s=_recall_decision_timeout
                ):
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
                            }, enabled=settings.filter_enabled)
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
                                        trace_builder.set_recall(used=True, question=second_question, facts_returned=0)

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
                                    }, enabled=settings.filter_enabled)
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
                            async for sse_chunk in yield_as_sse(second_data):
                                yield sse_chunk

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
        cap = capture_holder.get("capture")
        out_tokens = cap.output_tokens if cap else None
        await broadcast_response(
            session_id=session_id, turn_number=turn_number,
            status=200, latency_ms=0.0, output_tokens=out_tokens,
        )
    background_tasks.add_task(_broadcast_streaming_response)

    async def _finalize_streaming_trace_and_extraction():
        cap = capture_holder.get("capture")
        response_text = cap.get_full_text() if cap else ""
        # D8: a tool-call-only final message has empty content; fall back to a
        # tool_call summary so extraction is not skipped on empty response_text.
        if not response_text and cap and cap.tool_calls:
            response_text = _summarize_tool_calls(cap.tool_calls)
        stream_output_tokens = cap.output_tokens if cap else None
        _is_user_turn = bool(messages) and messages[-1].get("role") == "user"

        if trace_builder:
            trace_builder.set_response(
                status=200,
                latency_ms=0.0,
                output_tokens=stream_output_tokens,
                response_summary=response_text,
                cache_hit_tokens=cap.cache_hit_tokens if cap else 0,
                cache_miss_tokens=cap.cache_miss_tokens if cap else 0,
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
                    is_user_turn=_is_user_turn,
                    response_finish_reason=cap.finish_reason if cap else None,
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
