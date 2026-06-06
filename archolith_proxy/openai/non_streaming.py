"""Non-streaming response handler — retry, recall interception, extraction dispatch."""

from __future__ import annotations

import json
import time

import httpx
import structlog
from fastapi import Request
from starlette.background import BackgroundTasks
from starlette.responses import Response

from archolith_proxy.config import get_settings
from archolith_proxy.metrics import record_metric
from archolith_proxy.openai.errors import make_error_response
from archolith_proxy.openai.extraction import _run_extraction
from archolith_proxy.openai.helpers import (
    _extract_finish_reason,
    _extract_response_text,
)
from archolith_proxy.proxy.live import broadcast_response, broadcast_recall
from archolith_proxy.proxy.upstream import upstream_request_with_retry
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()


def _schedule_trace_store(
    trace_builder: TraceBuilder | None,
    background_tasks: BackgroundTasks,
) -> None:
    """Schedule a background task to store the turn trace."""
    if not trace_builder:
        return
    trace_builder.finalize_timing(time.monotonic())

    async def _store():
        try:
            trace = trace_builder.build()
            await get_trace_store().record(trace)
        except Exception:
            logger.warning("trace_store_failed_non_stream", exc_info=True)

    background_tasks.add_task(_store)


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
    synthetic_injected: bool = False,
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

    # Log upstream error responses for diagnosis (before any further processing)
    if resp.status_code >= 400:
        logger.warning(
            "upstream_error_response",
            status_code=resp.status_code,
            response_body=resp.text[:2000],
            session_id=session_id,
            synthetic_injected=synthetic_injected,
            request_preview=body[:500].decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)[:500],
        )

    # Predictive gate inputs for background curator pass
    _ns_is_user_turn = bool(messages) and messages[-1].get("role") == "user"

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
                        is_user_turn=_ns_is_user_turn,
                        response_finish_reason=_extract_finish_reason(final_data),
                    )
        except Exception as e:
            logger.warning("recall_interception_failed", session_id=session_id, error=str(e), exc_info=True)
            # Fall through to use original response

    # Handle synthetic tool calls (recall_session_work, recall_files_read).
    # Only runs if recall did not already produce a final_data (avoid double-processing).
    synthetic_result = None
    if synthetic_injected and session_id and final_data is None:
        try:
            from archolith_proxy.proxy.synthetic_tools import handle_non_streaming_synthetic

            synthetic_result = await handle_non_streaming_synthetic(
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

            if synthetic_result.synthetic_used and synthetic_result.final_data is not None:
                final_data = synthetic_result.final_data

                # Circuit breaker: record success or failure
                from archolith_proxy.proxy.circuit_breaker import record_synthetic_success, record_synthetic_failure
                if synthetic_result.fallback_used:
                    # Re-send failed; fallback strip was applied
                    record_synthetic_failure(
                        session_id,
                        max_consecutive=settings.synthetic_circuit_max_consecutive,
                        cooldown_seconds=settings.synthetic_circuit_cooldown_s,
                        max_total=settings.synthetic_circuit_max_total,
                    )
                else:
                    record_synthetic_success(session_id)

                # Schedule extraction for the final re-sent response
                if trace_builder:
                    trace_builder.set_response(
                        status=resp.status_code,
                        latency_ms=0.0,
                        output_tokens=None,
                        response_summary=_extract_response_text(final_data),
                    )

                response_text = _extract_response_text(final_data)
                if session_id and response_text:
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
                        is_user_turn=_ns_is_user_turn,
                        response_finish_reason=_extract_finish_reason(final_data),
                    )

        except Exception as e:
            logger.warning("synthetic_interception_failed", session_id=session_id, error=str(e), exc_info=True)
            # Circuit breaker: record failure
            from archolith_proxy.proxy.circuit_breaker import record_synthetic_failure
            record_synthetic_failure(
                session_id,
                max_consecutive=settings.synthetic_circuit_max_consecutive,
                cooldown_seconds=settings.synthetic_circuit_cooldown_s,
                max_total=settings.synthetic_circuit_max_total,
            )
            # Fall through to use original response

    # Handle native Read tool call interception (transparent cache serving).
    # Only runs if neither recall nor synthetic produced a final_data.
    native_intercept_result = None
    if synthetic_injected and session_id and final_data is None:
        try:
            from archolith_proxy.proxy.tool_intercept import handle_native_read_intercept

            native_intercept_result = await handle_native_read_intercept(
                resp=resp,
                http_client=request.app.state.http_client,
                url=url,
                headers=headers,
                body=body,
                session_id=session_id,
                turn_number=turn_number,
                original_messages=messages or [],
            )

            if native_intercept_result.intercepted and native_intercept_result.final_data is not None:
                final_data = native_intercept_result.final_data

                if trace_builder:
                    trace_builder.set_response(
                        status=resp.status_code,
                        latency_ms=0.0,
                        output_tokens=None,
                        response_summary=_extract_response_text(final_data),
                    )

                response_text = _extract_response_text(final_data)
                if session_id and response_text:
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
                        is_user_turn=_ns_is_user_turn,
                        response_finish_reason=_extract_finish_reason(final_data),
                    )

        except Exception as e:
            logger.warning("native_read_interception_failed", session_id=session_id, error=str(e), exc_info=True)
            # Fall through to use original response

    # Live stream: broadcast response event (non-streaming — always, for both recall and normal)
    await broadcast_response(
        session_id=session_id, turn_number=turn_number,
        status=resp.status_code, latency_ms=0.0, output_tokens=None,
    )

    # Schedule extraction for the original response if no interception was used
    _interception_used = (
        (recall_result and recall_result.recall_used and final_data is not None)
        or (synthetic_result and synthetic_result.synthetic_used and final_data is not None)
        or (native_intercept_result and native_intercept_result.intercepted and final_data is not None)
    )
    if session_id and not _interception_used:
        try:
            data = resp.json()
            response_text = _extract_response_text(data)
            _ns_finish = _extract_finish_reason(data)

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
                    is_user_turn=_ns_is_user_turn,
                    response_finish_reason=_ns_finish,
                )
        except Exception as e:
            logger.warning("non_streaming_extraction_setup_failed", error=str(e))

    # Trace: record upstream response (non-streaming) — always stored
    if trace_builder:
        if not _interception_used:
            # Normal path: trace the original response with usage data
            try:
                ns_data = resp.json() if resp.status_code == 200 else {}
            except Exception:
                ns_data = {}
            ns_usage = ns_data.get("usage", {})
            trace_builder.set_response(
                status=resp.status_code,
                latency_ms=0.0,
                output_tokens=ns_usage.get("completion_tokens") or ns_usage.get("output_tokens"),
                response_summary=response_text,
                cache_hit_tokens=ns_usage.get("prompt_cache_hit_tokens", 0) or 0,
                cache_miss_tokens=ns_usage.get("prompt_cache_miss_tokens", 0) or 0,
            )

    # Build and return the response
    if final_data is not None:
        # Recall/synthetic path: return the final data from interception
        # Strip internal proxy tools from the response so the client never sees them
        try:
            from archolith_proxy.proxy.tool_injection import strip_recall_from_response
            strip_recall_from_response(final_data)
        except Exception:
            pass
        try:
            from archolith_proxy.proxy.synthetic_tools import strip_synthetic_from_response
            strip_synthetic_from_response(final_data)
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
            _schedule_trace_store(trace_builder, background_tasks)

            return Response(
                content=json.dumps(data).encode(),
                status_code=resp.status_code,
                media_type="application/json",
                background=background_tasks,
            )
        except Exception:
            pass  # Fall back to original response

    # Trace: store turn trace as background task (fallback passthrough)
    _schedule_trace_store(trace_builder, background_tasks)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type="application/json",
        background=background_tasks,
    )
