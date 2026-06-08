"""Chat completions endpoint — main orchestrator for OpenAI-compatible proxy.

Routes requests through session resolution, context assembly (curator),
streaming/non-streaming dispatch, and async extraction.

Heavy logic is delegated to sub-modules: helpers, streaming, non_streaming,
extraction, file_cache.
"""

from __future__ import annotations

import json
import time

import httpx
import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import Response, StreamingResponse

from archolith_proxy.config import (
    SESSION_CONFIG_DENYLIST,
    build_effective_settings,
    get_settings,
    set_session_settings,
)
from archolith_proxy.curator.pipeline import curate_context
from archolith_proxy.graph.backend import get_backend, is_graph_ready
from archolith_proxy.metrics import record_metric
from archolith_proxy.openai.schemas import ChatCompletionRequest
from archolith_proxy.openai.helpers import (
    _build_call_map,  # noqa: F401 — re-exported for test compatibility
    _collect_tool_call_records,  # noqa: F401 — re-exported for test compatibility
    _extract_response_text,
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
from archolith_proxy.openai.errors import make_error_response, UpstreamError
from archolith_proxy.proxy.live import broadcast_request, broadcast_session_event
from archolith_proxy.proxy.session import resolve_session
from archolith_proxy.proxy.upstream import upstream_request_with_retry
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()

router = APIRouter()


# ── Passthrough helpers ─────────────────────────────────────────────────


def _estimate_input_tokens(messages: list[dict]) -> int:
    """Rough token estimate for input messages."""
    return sum(len(json.dumps(m)) // 4 for m in messages)


async def _handle_passthrough_stream(
    request: Request, body: dict, req: ChatCompletionRequest,
    trace_builder: TraceBuilder, background_tasks: BackgroundTasks,
    settings, request_start: float,
) -> Response:
    """Handle streaming passthrough (no context management)."""
    from archolith_proxy.proxy.streaming import ResponseCapture

    clean_model = req.model[: -len("-passthrough")]
    body["model"] = clean_model
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    upstream_headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }
    if req.stream:
        body.setdefault("stream_options", {})["include_usage"] = True
    request_body = json.dumps(body).encode("utf-8")
    t0 = time.monotonic()
    http_client = request.app.state.http_client
    pt_capture = ResponseCapture()

    async def _passthrough_stream():
        try:
            async with http_client.stream(
                "POST", upstream_url, headers=upstream_headers,
                content=request_body,
                timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    for line in chunk.decode("utf-8", errors="replace").split("\n"):
                        line = line.strip()
                        if line.startswith("data: ") and line != "data: [DONE]":
                            pt_capture.add_chunk(line[6:])
                    yield chunk
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("passthrough_stream_error", error=str(exc))

    latency_ms = (time.monotonic() - t0) * 1000

    async def _finalize_passthrough_trace():
        trace_builder.set_response(
            status=200, latency_ms=latency_ms,
            output_tokens=pt_capture.output_tokens,
            response_summary="(passthrough streaming)",
            cache_hit_tokens=pt_capture.cache_hit_tokens,
            cache_miss_tokens=pt_capture.cache_miss_tokens,
        )
        trace_builder.finalize_timing(time.monotonic())
        try:
            await get_trace_store().record(trace_builder.build())
        except Exception:
            pass

    background_tasks.add_task(_finalize_passthrough_trace)
    return StreamingResponse(
        _passthrough_stream(), status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        background=background_tasks,
    )


async def _handle_passthrough_non_stream(
    request: Request, body: dict, req: ChatCompletionRequest,
    trace_builder: TraceBuilder, background_tasks: BackgroundTasks,
    settings, request_start: float,
) -> Response:
    """Handle non-streaming passthrough."""
    clean_model = req.model[: -len("-passthrough")]
    body["model"] = clean_model
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    upstream_headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }
    request_body = json.dumps(body).encode("utf-8")
    t0 = time.monotonic()
    try:
        resp = await upstream_request_with_retry(
            client=request.app.state.http_client, url=upstream_url,
            headers=upstream_headers, content=request_body,
            max_retries=settings.upstream_max_retries,
            backoff_base=settings.upstream_retry_backoff_base_s,
        )
    except httpx.TimeoutException:
        return make_error_response(504, "Upstream request timed out", "upstream_timeout", code="timeout")
    except httpx.ConnectError as e:
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    latency_ms = (time.monotonic() - t0) * 1000
    response_data = resp.json() if resp.status_code == 200 else {}
    usage = response_data.get("usage", {})
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
    trace_builder.set_response(
        status=resp.status_code, latency_ms=latency_ms,
        output_tokens=output_tokens,
        response_summary=_extract_response_text(response_data)[:500],
        cache_hit_tokens=usage.get("prompt_cache_hit_tokens", 0) or 0,
        cache_miss_tokens=usage.get("prompt_cache_miss_tokens", 0) or 0,
    )
    trace_builder.finalize_timing(time.monotonic())

    async def _finalize_trace():
        try:
            await get_trace_store().record(trace_builder.build())
        except Exception:
            pass

    background_tasks.add_task(_finalize_trace)
    return Response(
        content=resp.content, status_code=resp.status_code,
        media_type="application/json", background=background_tasks,
    )


# ── Per-session config overlay ──────────────────────────────────────────


async def _clear_session_overlay():
    """Request-scoped dependency: clear the per-session settings overlay after the
    response is sent (including its background tasks) so it never bleeds into the
    next request sharing this task's context. In production each request runs in
    its own task (overlay is task-local), but the test client shares a context, so
    an explicit reset keeps behavior correct in both."""
    try:
        yield
    finally:
        set_session_settings(None)


async def _apply_session_config_overlay(header_value: str | None, session_id: str, settings):
    """Merge an X-Session-Config header into the session's persisted overrides,
    persist the merge, and activate the per-session settings overlay.

    Returns the effective settings (the overlay if any overrides apply, else the
    settings passed in). Denylisted and unknown fields are rejected loudly (logged,
    not silently dropped) and never persisted. The contextvar is reset by the
    _clear_session_overlay dependency after the response.
    """
    backend = get_backend()

    # 1. Apply an inbound header (launch-with-config or mutate-this-session).
    if header_value:
        incoming = None
        try:
            parsed = json.loads(header_value)
            if not isinstance(parsed, dict):
                raise ValueError("X-Session-Config must be a JSON object")
            incoming = parsed
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("session_config_header_invalid", session_id=session_id, error=str(e))

        if incoming is not None:
            denied = sorted(k for k in incoming if k in SESSION_CONFIG_DENYLIST)
            unknown = sorted(k for k in incoming if k not in SESSION_CONFIG_DENYLIST and not hasattr(settings, k))
            if denied:
                logger.warning("session_config_denied_fields", session_id=session_id, fields=denied)
            if unknown:
                logger.warning("session_config_unknown_fields", session_id=session_id, fields=unknown)

            existing_json = await backend.get_session_config_overrides(session_id)
            try:
                merged = json.loads(existing_json) if existing_json else {}
                if not isinstance(merged, dict):
                    merged = {}
            except (ValueError, json.JSONDecodeError):
                merged = {}
            applied = {
                k: v for k, v in incoming.items()
                if k not in SESSION_CONFIG_DENYLIST and hasattr(settings, k)
            }
            if applied:
                merged.update(applied)
                await backend.set_session_config_overrides(session_id, json.dumps(merged))
                logger.info("session_config_applied", session_id=session_id, fields=sorted(applied))

    # 2. Load the session's effective overrides and activate the overlay.
    overrides_json = await backend.get_session_config_overrides(session_id)
    if not overrides_json:
        return settings
    try:
        overrides = json.loads(overrides_json)
    except (ValueError, json.JSONDecodeError):
        logger.warning("session_config_load_corrupt", session_id=session_id)
        return settings
    if not isinstance(overrides, dict) or not overrides:
        return settings

    effective = build_effective_settings(overrides)
    set_session_settings(effective)
    return effective


# ── Main entry point ────────────────────────────────────────────────────


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

    # ── Passthrough mode ──
    _PASSTHROUGH_SUFFIX = "-passthrough"
    is_passthrough = req.model.endswith(_PASSTHROUGH_SUFFIX)
    if is_passthrough:
        from archolith_proxy.proxy.session import get_benchmark_passthrough_session_id

        clean_model = req.model[: -len(_PASSTHROUGH_SUFFIX)]
        body["model"] = clean_model
        input_tokens = _estimate_input_tokens(body.get("messages", []))
        passthrough_session_id = get_benchmark_passthrough_session_id()
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
                first_user_msg = ""
                for msg in messages_raw:
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                        first_user_msg = content[:200]
                        break
                if first_user_msg:
                    goal = first_user_msg.split("\n")[0].strip()[:120]
                    try:
                        await get_backend().update_goal(session_id, goal)
                        await broadcast_session_event(session_id, "session_created", goal=goal)
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
    user_turn_count = sum(1 for m in messages if m.get("role") == "user")
    is_user_turn = bool(messages) and messages[-1].get("role") == "user"

    session_over_budget = False
    if session_id and settings.max_input_tokens_per_session > 0:
        from archolith_proxy.proxy.circuit_breaker import add_session_tokens, is_session_over_budget
        if is_user_turn:
            add_session_tokens(session_id, input_tokens)
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
    if assembly_eligible and input_tokens < settings.assembly_min_input_tokens:
        assembly_eligible = False
        assembly_reason = "below_assembly_min_input_tokens"
        logger.info(
            "assembly_savings_gate_skip",
            session_id=session_id,
            input_tokens=input_tokens,
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
    # Without this, normal (non-"-passthrough") requests never called
    # set_assembly, so the trace always defaulted to mode="passthrough" with
    # 0 savings — even when agent-solo or the curator compressed heavily. The
    # only prior set_assembly call lived on the -passthrough branch.
    # Use the per-block values already computed above (passthrough leaves them at
    # 0, preserving the existing "passthrough => rewritten_tokens == 0" invariant).
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


