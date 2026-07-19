"""Curator pipeline — background pass, inline briefing, and main entry point.

Extracted from __init__.py to keep the module public surface clean.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from openai import AsyncOpenAI

from archolith_proxy.config import get_settings
from archolith_proxy.curator.briefing import (
    SessionBriefing, format_briefing_for_prompt, build_briefing_from_result,
)
from archolith_proxy.curator.prompts import CURATOR_SYSTEM_PROMPT, build_curator_user_prompt
from archolith_proxy.curator.state import (
    CuratorSnapshot,
    cache_briefing,
    cache_snapshot,
    get_briefing,
    get_snapshot,
    is_briefing_fresh,
)
from archolith_proxy.models.dtos import AssembledContext

logger = structlog.get_logger()

# Side-channel for curator failure data
# THREAD-SAFETY: safe under single asyncio event loop
_last_attempt: dict[str, dict] = {}


def get_last_attempt(session_id: str) -> dict | None:
    """Pop the last curator attempt info for a session."""
    return _last_attempt.pop(session_id, None)


def prune_last_attempts(active_session_ids: set[str]) -> int:
    """Drop last-attempt diagnostics for sessions no longer active.

    Recoverable: regenerated on the next curator run for the session.
    """
    stale = [sid for sid in _last_attempt if sid not in active_session_ids]
    for sid in stale:
        _last_attempt.pop(sid, None)
    return len(stale)




async def _run_background_pass_inner(
    settings,
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    messages: list[dict],
    *,
    bp_trace,
) -> None:
    """Inner body of run_background_pass."""
    debounce_s = settings.background_pass_debounce_ms / 1000
    bp_trace.debounce_ms = settings.background_pass_debounce_ms
    await asyncio.sleep(debounce_s)

    model = settings.curator_model or settings.extractor_model
    base_url = settings.curator_base_url or settings.extractor_base_url
    api_key = settings.curator_api_key or settings.extractor_api_key

    if not api_key:
        bp_trace.outcome = "failed"
        bp_trace.failure_detail = "no_api_key"
        bp_trace.completed_at = time.time()
        return

    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass

    previous_snapshot = get_snapshot(session_id)

    user_prompt = build_curator_user_prompt(
        session_goal, user_message, messages=messages,
        coherence_tail_size=settings.coherence_tail_size,
        max_tail_messages=settings.max_tail_messages,
        checkpoint=checkpoint, previous_snapshot=previous_snapshot,
    )
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    t0 = time.monotonic()
    budget_s = settings.background_pass_latency_budget_ms / 1000

    from archolith_proxy.curator.loop import _run_curator_native

    try:
        result, _bg_tool_log, _bg_failure = await asyncio.wait_for(
            _run_curator_native(
                client=client, session_id=session_id, user_prompt=user_prompt,
                max_iterations=settings.background_pass_max_iterations,
                system_prompt=CURATOR_SYSTEM_PROMPT, model=model,
            ),
            timeout=budget_s,
        )
    except asyncio.TimeoutError:
        bp_trace.outcome = "timeout"
        bp_trace.latency_ms = (time.monotonic() - t0) * 1000
        bp_trace.completed_at = time.time()
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        bp_trace.outcome = "failed"
        bp_trace.failure_detail = str(exc)[:500]
        bp_trace.latency_ms = (time.monotonic() - t0) * 1000
        bp_trace.completed_at = time.time()
        return

    latency_ms = (time.monotonic() - t0) * 1000

    if _bg_tool_log:
        bp_trace.tool_log = [
            {"tool": tc.tool, "status": tc.status, "args": tc.args,
             "result_preview": (tc.result_preview or "")[:200], "error": tc.error or ""}
            for tc in _bg_tool_log
        ]
        bp_trace.tool_calls_count = len(_bg_tool_log)

    if result is None:
        bp_trace.outcome = "no_result"
        bp_trace.latency_ms = latency_ms
        bp_trace.completed_at = time.time()
        return

    briefing = build_briefing_from_result(
        result=result, session_id=session_id, turn_number=turn_number,
        latency_ms=latency_ms, session_goal=session_goal, messages=messages,
        mode="two_pass",
    )
    cache_briefing(session_id, briefing)

    bp_trace.outcome = "success"
    bp_trace.latency_ms = latency_ms
    bp_trace.completed_at = time.time()
    bp_trace.files_fetched = len(briefing.files)
    bp_trace.context_chars = len(result.context_text)
    bp_trace.briefing_cached = True
    bp_trace.prompt_tokens_used = result.prompt_tokens_used
    bp_trace.completion_tokens_used = result.completion_tokens_used

    try:
        from archolith_proxy.metrics import record_metric
        record_metric("background_pass_successes", 1)
    except Exception:
        pass

    logger.info("background_pass_complete", session_id=session_id, turn=turn_number,
                tool_calls=result.tool_calls_used, latency_ms=round(latency_ms, 1),
                files=len(briefing.files), context_len=len(result.context_text))


async def run_background_pass(
    session_id: str, turn_number: int, user_message: str,
    session_goal: str | None, messages: list[dict],
) -> None:
    """Run the background curator pass after a response completes."""
    settings = get_settings()

    if not settings.background_pass_enabled or not settings.curator_enabled:
        return

    from archolith_proxy.curator import _background_pass_fn
    from archolith_proxy.models.dtos import BackgroundPassTrace
    from archolith_proxy.trace.store import get_trace_store

    bp_trace = BackgroundPassTrace(session_id=session_id, trigger_turn=turn_number)

    try:
        if _background_pass_fn is not None:
            briefing = await _background_pass_fn(
                session_id, turn_number, user_message, session_goal, messages,
            )
            if briefing:
                cache_briefing(session_id, briefing)
                bp_trace.outcome = "success"
                bp_trace.briefing_cached = True
                bp_trace.files_fetched = len(briefing.files)
                bp_trace.context_chars = len(briefing.context_block)
                bp_trace.completed_at = time.time()
                bp_trace.latency_ms = (time.time() - bp_trace.started_at) * 1000
                # Count the success in the registered-fn (two_curator / prepper)
                # path too — previously only the default _run_background_pass_inner
                # path incremented this, so two_curator showed 0 despite firing.
                try:
                    from archolith_proxy.metrics import record_metric
                    record_metric("background_pass_successes", 1)
                except Exception:
                    pass
            else:
                bp_trace.outcome = "no_result"
                bp_trace.completed_at = time.time()
        else:
            await _run_background_pass_inner(
                settings, session_id, turn_number, user_message, session_goal,
                messages, bp_trace=bp_trace,
            )
    except asyncio.CancelledError:
        bp_trace.outcome = "cancelled"
        bp_trace.cancel_reason = "superseded_by_next_turn"
        bp_trace.completed_at = time.time()
        bp_trace.latency_ms = (time.time() - bp_trace.started_at) * 1000
    finally:
        try:
            await get_trace_store().record_bg_pass(bp_trace)
        except Exception:
            pass


async def _run_with_briefing(
    session_id: str, turn_number: int, user_message: str,
    session_goal: str | None, briefing: SessionBriefing,
    messages: list[dict], client: AsyncOpenAI, model: str, settings,
) -> AssembledContext | None:
    """Inline pass with pre-built briefing injection."""
    from archolith_proxy.curator.loop import _run_curator_native

    briefing_text = format_briefing_for_prompt(briefing)

    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass

    previous_snapshot = get_snapshot(session_id)

    base_prompt = build_curator_user_prompt(
        session_goal, user_message, messages=messages,
        coherence_tail_size=settings.coherence_tail_size,
        max_tail_messages=settings.max_tail_messages,
        checkpoint=checkpoint, previous_snapshot=previous_snapshot,
    )

    user_prompt = briefing_text + "\n\n---\n\n" + base_prompt

    inline_latency_budget = min(settings.curator_latency_budget_ms, 3000)
    try:
        result_tuple = await asyncio.wait_for(
            _run_curator_native(
                client=client, session_id=session_id, user_prompt=user_prompt,
                max_iterations=2, system_prompt=CURATOR_SYSTEM_PROMPT, model=model,
            ),
            timeout=inline_latency_budget / 1000,
        )
        result, _inl_tool_log, _inl_failure = result_tuple
    except asyncio.TimeoutError:
        _last_attempt[session_id] = {
            "tool_log": [],
            "failure_reason": "inline_timeout",
        }
        return None
    except Exception as exc:
        _last_attempt[session_id] = {
            "tool_log": [],
            "failure_reason": f"inline_exception: {str(exc)[:200]}",
        }
        return None

    if result is None:
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in _inl_tool_log],
            "failure_reason": f"inline_{_inl_failure or 'no_result'}",
        }
        return None

    _max_summary = 2000
    cache_snapshot(session_id, CuratorSnapshot(
        curated_paths=tuple(sorted(result.curated_paths)),
        retained_turn_numbers=tuple(result.retained_turn_numbers) if result.retained_turn_numbers else None,
        context_summary=result.context_text[:_max_summary],
        tool_calls_used=result.tool_calls_used,
        turn_number=turn_number,
    ))

    return AssembledContext(
        system_message={"role": "system", "content": result.context_text},
        graph_context=[{"role": "system", "content": result.context_text}],
        coherence_tail=[], token_estimate=result.estimated_tokens,
        facts_retrieved=result.tool_calls_used, session_id=session_id,
        files_selected=[{"path": p} for p in result.curated_paths],
        decisions_selected=[], compression_ratio=1.0,
        retained_turn_numbers=result.retained_turn_numbers,
        curator_tool_log=[tc.to_dict() for tc in result.tool_log],
        curator_prompt_tokens=result.prompt_tokens_used,
        curator_completion_tokens=result.completion_tokens_used,
        curator_cached_tokens=result.cached_tokens_used,
    )


async def curate_context(
    session_id: str, turn_number: int, user_message: str,
    session_goal: str | None, http_client, messages: list[dict],
) -> AssembledContext | None:
    """Run the curator LLM to build a context block.

    Two-pass dispatch:
    1. Fresh briefing → inline pass (1-2 iterations)
    2. Stale briefing → inline pass with staleness warning
    3. No briefing → full curator loop
    """
    settings = get_settings()

    if not settings.curator_enabled or not settings.file_cache_enabled:
        return None

    user_turns = sum(1 for m in messages if m.get("role") == "user")
    if user_turns < settings.cold_start_turns:
        return None

    model = settings.curator_model or settings.extractor_model
    base_url = settings.curator_base_url or settings.extractor_base_url
    api_key = settings.curator_api_key or settings.extractor_api_key
    if not api_key:
        return None

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    # --- Two-pass dispatch ---
    briefing = get_briefing(session_id)
    fresh = is_briefing_fresh(session_id, turn_number)

    # Phase 0: record briefing staleness at hot-path read (ledger_lag proxy).
    if briefing is not None:
        from archolith_proxy.metrics import record_metric
        record_metric("hot_path_briefing_lag_sum", max(0, turn_number - briefing.source_turn))
        record_metric("hot_path_briefing_lag_count", 1)

    if briefing and (fresh or briefing.source_turn >= turn_number - settings.briefing_max_staleness):
        from archolith_proxy.curator import _inline_pass_fn
        from archolith_proxy.metrics import record_metric

        # The deterministic assembler (Phase 2) serves this read with NO LLM call,
        # so only count a hot-path LLM call when the inline read actually uses one.
        _deterministic_read = (
            settings.curation_mode == "two_curator" and settings.assembler_deterministic
        )
        if not _deterministic_read:
            record_metric("hot_path_llm_calls", 1)
        if _inline_pass_fn is not None:
            result = await _inline_pass_fn(
                session_id, turn_number, user_message, session_goal,
                briefing, messages, client, model, settings,
            )
        else:
            result = await _run_with_briefing(
                session_id=session_id, turn_number=turn_number,
                user_message=user_message, session_goal=session_goal,
                briefing=briefing, messages=messages, client=client,
                model=model, settings=settings,
            )
        if result is not None:
            return result

    # --- Synchronous prepper top-up (flexible, off by default) ---
    # We only reach here when no usable briefing served the turn above. If enabled,
    # block on ONE bounded LIGHT prepper pass (metadata/facts only, no file-fetch)
    # and retry the (cheap, deterministic) inline read on the fresh briefing,
    # instead of falling through to the expensive full loop. The full background
    # prepper is ~10-30s — too slow to block the hot path on; the light pass
    # finishes inside prepper_block_budget_ms. Cost: light-prepper latency on miss
    # turns only. Works whether or not the background worker is enabled — it calls
    # run_light_prepper directly, so a "synchronous-only" configuration is valid.
    if settings.prepper_block_on_miss:
        from archolith_proxy.curator.prepper import run_light_prepper
        from archolith_proxy.curator import _inline_pass_fn
        from archolith_proxy.metrics import record_metric

        _block_budget_s = max(0.1, settings.prepper_block_budget_ms / 1000)
        try:
            _topped = await asyncio.wait_for(
                run_light_prepper(session_id, turn_number, user_message, session_goal, messages),
                timeout=_block_budget_s,
            )
            if _topped:
                cache_briefing(session_id, _topped)
        except asyncio.TimeoutError:
            record_metric("prepper_block_timeouts", 1)
            logger.info("prepper_block_topup_timeout", session_id=session_id, turn=turn_number)
        except Exception:
            logger.debug("prepper_block_topup_failed", session_id=session_id, exc_info=True)

        briefing = get_briefing(session_id)
        if briefing is not None and (
            is_briefing_fresh(session_id, turn_number)
            or briefing.source_turn >= turn_number - settings.briefing_max_staleness
        ):
            record_metric("prepper_block_topups", 1)
            _det = settings.curation_mode == "two_curator" and settings.assembler_deterministic
            if not _det:
                record_metric("hot_path_llm_calls", 1)
            if _inline_pass_fn is not None:
                result = await _inline_pass_fn(
                    session_id, turn_number, user_message, session_goal,
                    briefing, messages, client, model, settings,
                )
            else:
                result = await _run_with_briefing(
                    session_id=session_id, turn_number=turn_number,
                    user_message=user_message, session_goal=session_goal,
                    briefing=briefing, messages=messages, client=client,
                    model=model, settings=settings,
                )
            if result is not None:
                return result

    # --- Full curator loop ---
    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass

    previous_snapshot = get_snapshot(session_id)

    user_prompt = build_curator_user_prompt(
        session_goal, user_message, messages=messages,
        coherence_tail_size=settings.coherence_tail_size,
        max_tail_messages=settings.max_tail_messages,
        checkpoint=checkpoint, previous_snapshot=previous_snapshot,
    )

    attempt_tool_log: list = []
    attempt_failure: str = ""

    from archolith_proxy.curator.loop import _run_curator_native
    from archolith_proxy.metrics import record_metric

    record_metric("hot_path_llm_calls", 1)
    try:
        result_tuple = await asyncio.wait_for(
            _run_curator_native(
                client=client, session_id=session_id, user_prompt=user_prompt,
                max_iterations=settings.curator_max_iterations,
                system_prompt=CURATOR_SYSTEM_PROMPT, model=model,
            ),
            timeout=settings.curator_latency_budget_ms / 1000,
        )
        result, attempt_tool_log, attempt_failure = result_tuple
    except asyncio.TimeoutError:
        from archolith_proxy.metrics import record_metric
        record_metric("curator_timeouts", 1)
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in attempt_tool_log],
            "failure_reason": "timeout",
        }
        return None
    except Exception as exc:
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in attempt_tool_log],
            "failure_reason": f"exception: {str(exc)[:200]}",
        }
        return None

    if result is None:
        from archolith_proxy.metrics import record_metric
        record_metric("curator_fallbacks", 1)
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in attempt_tool_log],
            "failure_reason": attempt_failure,
        }
        return None

    _max_summary = 2000
    cache_snapshot(session_id, CuratorSnapshot(
        curated_paths=tuple(sorted(result.curated_paths)),
        retained_turn_numbers=tuple(result.retained_turn_numbers) if result.retained_turn_numbers else None,
        context_summary=result.context_text[:_max_summary],
        tool_calls_used=result.tool_calls_used,
        turn_number=turn_number,
    ))

    return AssembledContext(
        system_message={"role": "system", "content": result.context_text},
        graph_context=[{"role": "system", "content": result.context_text}],
        coherence_tail=[], token_estimate=result.estimated_tokens,
        facts_retrieved=result.tool_calls_used, session_id=session_id,
        files_selected=[{"path": p} for p in result.curated_paths],
        decisions_selected=[], compression_ratio=1.0,
        retained_turn_numbers=result.retained_turn_numbers,
        curator_tool_log=[tc.to_dict() for tc in result.tool_log],
        curator_prompt_tokens=result.prompt_tokens_used,
        curator_completion_tokens=result.completion_tokens_used,
        curator_cached_tokens=result.cached_tokens_used,
        goal_drifted=goal_drifted,
        drift_turn=drift_turn,
    )


__all__ = ["curate_context", "run_background_pass", "get_last_attempt", "prune_last_attempts"]
