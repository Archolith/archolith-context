"""Chat completions endpoint for the OpenAI-compatible proxy."""

from __future__ import annotations

import asyncio
import json
import time

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response

from archolith_proxy.compliance import apply_session_consent
from archolith_proxy.config import get_settings
from archolith_proxy.curator.pipeline import curate_context
from archolith_proxy.graph.backend import get_backend, is_graph_ready
from archolith_proxy.metrics import record_assembly_mode, record_metric
from archolith_proxy.openai.schemas import ChatCompletionRequest
from archolith_proxy.openai.chat_overlay import (
    _apply_session_config_overlay as _apply_session_config_overlay_impl,
    _clear_session_overlay,
)
from archolith_proxy.openai.chat_passthrough import (
    _estimate_input_tokens,
    _handle_passthrough_non_stream,
    _handle_passthrough_stream,
)
from archolith_proxy.openai.helpers import (
    _build_call_map,  # noqa: F401 — re-exported for test compatibility
    _collect_tool_call_records,  # noqa: F401 — re-exported for test compatibility
    _extract_response_text,  # noqa: F401 — re-exported for test compatibility
    _extract_user_message,
    _infer_file_touch_statuses,  # noqa: F401 — re-exported for test compatibility
)
from archolith_proxy.openai.extraction import _run_extraction  # noqa: F401 — re-exported for test compatibility
from archolith_proxy.openai.file_cache import (
    _invalidate_file_cache,  # noqa: F401 — re-exported for test compatibility
    _invalidate_written_files,  # noqa: F401 — re-exported for test compatibility
    _upsert_file_cache,  # noqa: F401 — re-exported for file cache pipeline
)
from archolith_proxy.openai.non_streaming import _handle_non_streaming
from archolith_proxy.openai.streaming import _handle_streaming
from archolith_proxy.token_accounting import build_telemetry, extract_client_hint
from archolith_proxy.openai.errors import make_error_response, UpstreamError
from archolith_proxy.proxy.live import broadcast_request, broadcast_session_event
from archolith_proxy.proxy.session import resolve_session
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()

router = APIRouter()


async def _apply_session_config_overlay(header_value: str | None, session_id: str, settings):
    return await _apply_session_config_overlay_impl(header_value, session_id, settings, backend_factory=get_backend)


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    _overlay_cleanup: None = Depends(_clear_session_overlay),
) -> Response:
    """Accept OpenAI chat completion requests, forward to upstream,
    resolve session, assemble context, and trigger async fact extraction."""
    settings = get_settings()
    request_start = time.monotonic()

    trace_builder = TraceBuilder()
    trace_builder.set_request_start(request_start, time.time())

    try:
        body = await request.json()
    except Exception:
        return make_error_response(400, "Invalid JSON in request body", "invalid_request_error")

    try:
        req = ChatCompletionRequest(**body)
    except Exception as e:
        return make_error_response(400, f"Invalid request: {e}", "invalid_request_error")

    if not req.messages:
        return make_error_response(400, "Messages array must not be empty", "invalid_request_error", param="messages")

    apply_session_consent(request.headers)

    # ── Passthrough mode ──
    _PASSTHROUGH_SUFFIX = "-passthrough"
    is_passthrough = req.model.endswith(_PASSTHROUGH_SUFFIX)
    if is_passthrough:
        clean_model = req.model[: -len(_PASSTHROUGH_SUFFIX)]
        body["model"] = clean_model
        input_tokens = _estimate_input_tokens(body.get("messages", []))
        passthrough_session_id = (
            request.headers.get("x-session-id")
            or request.headers.get("X-Session-ID")
            or request.headers.get("x-session-affinity")
        )
        trace_builder.set_request(
            session_id=passthrough_session_id, turn_number=0, model=clean_model,
            stream=req.stream, input_tokens=input_tokens,
            message_count=len(body.get("messages", [])),
            user_turn_count=sum(1 for m in body.get("messages", []) if m.get("role") == "user"),
        )
        trace_builder.set_original_messages(body.get("messages", []))
        trace_builder.set_assembly(
            mode="passthrough", reason="passthrough model", latency_ms=0.0,
            facts_selected=[], files_selected=[], decisions_selected=[],
            rewritten_tokens=0, savings_tokens=0, savings_ratio=0.0, compression_ratio=1.0,
        )
        # Count passthrough in the process-level assembly_modes block, exactly
        # like a non-passthrough request — so the A/B passthrough arm is recorded
        # symmetrically and /metrics reflects passthrough traffic. (total_requests
        # is already counted by the HTTP middleware; input tokens are counted
        # post-branch for non-passthrough, so mirror that here.)
        record_assembly_mode("passthrough")
        record_metric("total_input_tokens_seen", input_tokens)
        if req.stream:
            return await _handle_passthrough_stream(
                request, body, req, trace_builder, background_tasks, settings, request_start,
            )
        return await _handle_passthrough_non_stream(
            request, body, req, trace_builder, background_tasks, settings, request_start,
        )

    # ── Session resolution ──
    session_id = None
    turn_number = 0
    graph_ready = is_graph_ready()

    if not hasattr(request.app.state, "http_client"):
        return make_error_response(503, "Proxy not initialized — lifespan did not complete", "server_error")

    if graph_ready:
        try:
            headers = {k: v for k, v in request.headers.items()}
            messages_raw = body.get("messages", [])
            session_id, is_new = await resolve_session(headers, messages_raw)
            turn_number = await get_backend().get_turn_number(session_id)

            from archolith_proxy.curator.state import cancel_background_task
            cancel_background_task(session_id)

            if is_new:
                try:
                    await broadcast_session_event(session_id, "session_created", goal=None)
                except Exception:
                    pass

            # Populate per-session trace metadata on the first turn AND after an
            # LRU eviction of a resumed session: repopulate when absent so
            # harness_env / proxy_config survive eviction instead of being lost
            # for the process lifetime. proxy_config presence is the sentinel
            # (always set; harness_env is conditional on the request).
            store = get_trace_store()
            if not await store.has_session_metadata(session_id, "proxy_config"):
                from archolith_proxy.proxy.session import extract_harness_env
                harness_env = extract_harness_env(messages_raw)
                if harness_env:
                    await store.set_session_metadata(session_id, "harness_env", harness_env)
                from archolith_proxy.config import snapshot_config
                await store.set_session_metadata(session_id, "proxy_config", snapshot_config())

            structlog.contextvars.bind_contextvars(session_id=session_id, turn_number=turn_number)

            # Per-session config overlay: merge any X-Session-Config header into
            # the session's persisted overrides and activate the effective
            # settings for the remainder of this request (and its async follow-up
            # work). Reset post-response by the _clear_session_overlay dependency.
            settings = await _apply_session_config_overlay(
                request.headers.get("X-Session-Config"), session_id, settings
            )
        except Exception as e:
            logger.warning("session_resolution_failed", error=str(e))
            record_metric("neo4j_errors", 1)

    # ── Session goal for extraction ──
    session_goal = None
    if session_id and graph_ready:
        try:
            session_data = await get_backend().find_session_by_id(session_id)
            session_goal = session_data.get("goal") if session_data else None
        except Exception:
            pass

    # ── Context assembly ──
    assembled = None
    assembly_mode = "passthrough"
    assembly_reason = ""
    assembly_latency_ms = 0.0
    rewritten_tokens = 0
    savings = 0
    savings_ratio = 0.0
    messages = body.get("messages", [])
    input_tokens = _estimate_input_tokens(messages)
    record_metric("total_input_tokens_seen", input_tokens)

    # Structural token accounting: counts tool schemas / tool_calls / framing the
    # crude content estimate above misses, so the assembly gate sees the true
    # request size. Run via to_thread — tiktoken releases the GIL, so encoding
    # does not block the event loop under concurrency.
    token_telemetry = await asyncio.to_thread(
        build_telemetry,
        messages,
        tools=body.get("tools"),
        client_reported_tokens=extract_client_hint(dict(request.headers), body),
        min_input_tokens=settings.assembly_min_input_tokens,
        min_savings_ratio=settings.assembly_min_savings_ratio,
        cold_start_turns=settings.cold_start_turns,
        cold_start_token_threshold=settings.cold_start_token_threshold,
        session_id=session_id or "",
        turn_number=turn_number,
    )
    gate_input_tokens = token_telemetry.breakdown.gate_input_tokens
    record_metric("total_input_tokens_structural", token_telemetry.breakdown.input_tokens_structural_est)
    if token_telemetry.breakdown.input_tokens_client_reported is not None:
        record_metric("total_input_tokens_client_reported", token_telemetry.breakdown.input_tokens_client_reported)
    _gate_source = getattr(token_telemetry.breakdown.gate_source, "value", "")
    if _gate_source:
        record_metric(f"gate_decisions_{_gate_source}", 1)
    trace_builder.set_token_telemetry(token_telemetry.breakdown)

    user_turn_count = sum(1 for m in messages if m.get("role") == "user")
    is_user_turn = bool(messages) and messages[-1].get("role") == "user"

    session_over_budget = False
    if session_id and settings.max_input_tokens_per_session > 0:
        from archolith_proxy.proxy.circuit_breaker import add_session_tokens, is_session_over_budget
        if is_user_turn:
            add_session_tokens(session_id, gate_input_tokens)
        session_over_budget = is_session_over_budget(session_id, settings.max_input_tokens_per_session)
        if session_over_budget and settings.session_token_budget_action == "reject":
            raise UpstreamError(
                f"Session {session_id} exceeded token budget "
                f"({settings.max_input_tokens_per_session:,} tokens)"
            )

    trace_builder.set_request(
        session_id=session_id, turn_number=turn_number, model=req.model,
        stream=req.stream, input_tokens=input_tokens,
        message_count=len(messages), user_turn_count=user_turn_count,
        is_user_turn=is_user_turn,
    )
    trace_builder.set_original_messages(body.get("messages", []), is_user_turn=is_user_turn)

    await broadcast_request(
        session_id=session_id, turn_number=turn_number, model=req.model,
        message_count=len(body.get("messages", [])), stream=req.stream,
        input_tokens=input_tokens,
    )

    # ── Agent-solo gating ──
    is_agent_solo = session_id and graph_ready and not session_over_budget and not is_user_turn
    if is_agent_solo:
        if settings.agent_solo_shrink_enabled or settings.agent_solo_dedup_enabled or settings.agent_solo_compress_middle_enabled:
            from archolith_proxy.proxy.agent_solo import compress_agent_solo

            messages, solo_stats = compress_agent_solo(
                messages=messages,
                session_id=session_id,
                input_tokens=input_tokens,
                shrink_enabled=settings.agent_solo_shrink_enabled,
                dedup_enabled=settings.agent_solo_dedup_enabled,
                compress_middle_enabled=settings.agent_solo_compress_middle_enabled,
                shrink_max_tokens=settings.agent_solo_shrink_max_tokens,
                min_input_tokens=settings.agent_solo_min_input_tokens,
                coherence_tail_size=settings.coherence_tail_size,
                max_tail_messages=settings.max_tail_messages,
            )
            solo_chars_saved = int(solo_stats.get("total_chars_saved", 0) or 0)
            trace_builder.set_solo_stats(solo_stats)
            savings += solo_chars_saved // 4
            rewritten_tokens += solo_chars_saved // 4
            assembly_mode = "agent_solo"
            assembly_reason = "agent_solo_compression"

    # ── Curator assembly (user turns) ──
    # Savings gate: skip the (expensive) curator assembly on short conversations
    # where a rewrite can't save enough to justify the cost/risk. Configurable via
    # assembly_min_input_tokens (tests set it to 0 to exercise assembly directly).
    assembly_eligible = bool(
        session_id and graph_ready and not session_over_budget and is_user_turn
    )
    if assembly_eligible and gate_input_tokens < settings.assembly_min_input_tokens:
        assembly_eligible = False
        assembly_reason = "below_assembly_min_input_tokens"
        logger.info(
            "assembly_savings_gate_skip",
            session_id=session_id,
            input_tokens=input_tokens,
            gate_input_tokens=gate_input_tokens,
            min_input_tokens=settings.assembly_min_input_tokens,
        )
    if assembly_eligible:
        t0 = time.monotonic()
        assembled = await curate_context(
            session_id=session_id, turn_number=turn_number,
            user_message=_extract_user_message(messages),
            session_goal=session_goal, http_client=request.app.state.http_client,
            messages=messages,
        )
        if assembled:
            assembly_mode = "curator"
            assembly_reason = "curator_context"
            assembly_latency_ms = (time.monotonic() - t0) * 1000
            assembled_tokens = assembled.token_estimate or 0
            savings = max(0, input_tokens - assembled_tokens)
            rewritten_tokens = input_tokens - savings
            savings_ratio = round(savings / input_tokens, 4) if input_tokens > 0 else 0.0
            record_metric("curator_calls", 1)

            # Record curator LLM token usage in trace and metrics
            if assembled.curator_prompt_tokens or assembled.curator_completion_tokens:
                trace_builder.set_helper_usage(
                    curator_prompt_tokens=assembled.curator_prompt_tokens,
                    curator_completion_tokens=assembled.curator_completion_tokens,
                    curator_cached_tokens=assembled.curator_cached_tokens,
                )
                record_metric("curator_prompt_tokens_total", assembled.curator_prompt_tokens)
                record_metric("curator_completion_tokens_total", assembled.curator_completion_tokens)
                record_metric("curator_cached_tokens_total", assembled.curator_cached_tokens)

    # ── Record curator skip reason when eligible but skipped/failed ──
    if session_id and graph_ready and not session_over_budget and is_user_turn and not assembled:
        from archolith_proxy.curator.pipeline import get_last_attempt
        _last = get_last_attempt(session_id)
        if _last:
            _curator_skip = _last.get("failure_reason", "unknown")
        elif not settings.curator_enabled or not settings.file_cache_enabled:
            _curator_skip = "disabled"
        elif user_turn_count < settings.cold_start_turns:
            _curator_skip = "cold_start"
        else:
            _curator_skip = "no_result"
        trace_builder.set_curator_skip_reason(_curator_skip)

    # ── Record final assembly outcome on the trace ──
    _comp_ratio = round((input_tokens - savings) / input_tokens, 4) if input_tokens > 0 else 1.0
    trace_builder.set_assembly(
        mode=assembly_mode,
        reason=assembly_reason,
        latency_ms=assembly_latency_ms,
        rewritten_tokens=rewritten_tokens,
        savings_tokens=savings,
        savings_ratio=savings_ratio,
        compression_ratio=_comp_ratio,
    )
    # Count the resolved mode in the process-level assembly_modes block so
    # /metrics reflects real traffic (previously never incremented -> always 0).
    record_assembly_mode(assembly_mode)

    # ── Inject assembled context and proxy tools ──
    if assembled and assembly_mode == "curator":
        from archolith_proxy.proxy.rewrite import rewrite_messages
        messages = rewrite_messages(
            messages, assembled,
            coherence_tail_size=settings.coherence_tail_size,
            max_tail_messages=settings.max_tail_messages,
        )
        body["messages"] = messages

    # ── Inject no-DSML hint for DeepSeek ──
    from archolith_proxy.proxy.rewrite import inject_no_dsml_hint
    body["messages"] = inject_no_dsml_hint(
        body.get("messages", []),
        model=req.model,
        has_tools=bool(body.get("tools")),
    )

    # ── Filtering ──
    from archolith_proxy.filter_adapter import filter_request_body, is_available as filter_is_available
    _filter_chars_before = sum(len(json.dumps(m)) for m in body.get("messages", []))
    body = filter_request_body(body, enabled=settings.filter_enabled)
    _filter_chars_after = sum(len(json.dumps(m)) for m in body.get("messages", []))
    trace_builder.set_filter_stats(
        available=filter_is_available(),
        chars_saved=max(0, _filter_chars_before - _filter_chars_after),
        chars_before=_filter_chars_before,
        chars_after=_filter_chars_after,
    )

    # ── Proxy-forced recall for key trigger patterns ──
    # This fires before the model receives the request so recall context is
    # already in the system message — the model does not need to invoke the
    # recall tool explicitly.  The model-invoked recall path is kept as a
    # fallback for queries the proxy triggers don't cover.
    if session_id and graph_ready and not session_over_budget and is_user_turn:
        from archolith_proxy.proxy.recall import detect_recall_trigger, inject_proxy_recall_into_body
        from archolith_proxy.proxy.tool_injection import handle_recall_tool_call
        _recall_trigger = detect_recall_trigger(body.get("messages", []), is_user_turn=is_user_turn)
        if _recall_trigger:
            _trigger_type, _trigger_query = _recall_trigger
            try:
                _recall_text = await handle_recall_tool_call(
                    http_client=request.app.state.http_client,
                    session_id=session_id,
                    question=_trigger_query,
                    turn_number=turn_number,
                )
                _recall_empty = not _recall_text or "No facts found" in _recall_text or "No relevant facts" in _recall_text
                if not _recall_empty:
                    body = inject_proxy_recall_into_body(body, _recall_text, _trigger_type)
                    _outbound_chars_sent = sum(len(json.dumps(m)) for m in body.get("messages", []))
                    trace_builder.set_outbound_context_stats(
                        outbound_chars_sent=_outbound_chars_sent,
                        proxy_recall_chars_added=max(0, _outbound_chars_sent - _filter_chars_after),
                    )
                    # Rough fact count: count lines that look like fact entries
                    _recall_fact_count = sum(1 for ln in _recall_text.splitlines() if ln.strip().startswith("- "))
                    trace_builder.set_recall(
                        used=True,
                        question=_trigger_query,
                        facts_returned=_recall_fact_count,
                        trigger=f"proxy_forced:{_trigger_type}",
                    )
                    record_metric("proxy_recall_injections", 1)
                    logger.info(
                        "proxy_recall_injected",
                        session_id=session_id,
                        trigger=_trigger_type,
                        query=_trigger_query[:80],
                        facts=_recall_fact_count,
                    )
            except Exception as _exc:
                logger.warning("proxy_recall_failed", session_id=session_id, error=str(_exc))

    # ── Inject synthetic session-summary tools ──
    synthetic_injected = False
    if settings.synthetic_tools_enabled and session_id:
        from archolith_proxy.proxy.circuit_breaker import is_synthetic_allowed
        if is_synthetic_allowed(session_id):
            from archolith_proxy.proxy.synthetic_tools import inject_synthetic_tools
            body = inject_synthetic_tools(body)
            synthetic_injected = True
        else:
            record_metric("synthetic_injections_skipped", 1)

    # ── Inject session recall tool ──
    if settings.session_recall_tool_enabled and session_id:
        from archolith_proxy.proxy.tool_injection import inject_recall_tool
        body = inject_recall_tool(body)

    # ── Build upstream request and dispatch ──
    request_body = json.dumps(body).encode("utf-8")
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    upstream_headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }

    if req.stream:
        body.setdefault("stream_options", {})["include_usage"] = True
        request_body = json.dumps(body).encode("utf-8")

    # Synthetic tools force non-streaming: intercept full response, then convert to SSE
    if req.stream and synthetic_injected:
        body["stream"] = False
        body.pop("stream_options", None)
        request_body = json.dumps(body).encode("utf-8")
        from archolith_proxy.proxy.streaming import _wrap_response_as_sse

        result = await _handle_non_streaming(
            request=request, background_tasks=background_tasks,
            url=upstream_url, headers=upstream_headers, body=request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
            recall_injected=bool(settings.session_recall_tool_enabled and session_id),
            synthetic_injected=True,
            session_goal=session_goal,
            trace_builder=trace_builder,
        )
        return _wrap_response_as_sse(result)

    if req.stream:
        return await _handle_streaming(
            request=request, background_tasks=background_tasks,
            url=upstream_url, headers=upstream_headers, body=request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
            recall_injected=bool(settings.session_recall_tool_enabled and session_id),
            synthetic_injected=synthetic_injected,
            session_goal=session_goal,
            trace_builder=trace_builder,
        )
    else:
        return await _handle_non_streaming(
            request=request, background_tasks=background_tasks,
            url=upstream_url, headers=upstream_headers, body=request_body,
            session_id=session_id, turn_number=turn_number,
            messages=body.get("messages", []),
            recall_injected=bool(settings.session_recall_tool_enabled and session_id),
            synthetic_injected=synthetic_injected,
            session_goal=session_goal,
            trace_builder=trace_builder,
        )


