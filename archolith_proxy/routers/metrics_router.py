"""Metrics endpoint — process-level counters and derived stats."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from archolith_proxy import __version__
from archolith_proxy.admin import require_admin_token
from archolith_proxy.config import get_settings
from archolith_proxy.graph.backend import is_graph_ready
from archolith_proxy.metrics import get_curator_phase_latency_percentiles, get_metrics
from archolith_proxy.trace.store import get_trace_store

router = APIRouter()


def _get_plugin_metrics() -> dict:
    """Return aggregated plugin metrics from the registry."""
    try:
        from archolith_proxy.plugins import get_plugin_registry

        registry = get_plugin_registry()
        raw = registry.aggregate_metrics()
        # Group by plugin ID: {"filter": {"hits": 5}, "audit": {...}}
        grouped: dict[str, dict[str, int | float]] = {}
        for key, value in raw.items():
            parts = key.split(".", 2)  # "plugins.<id>.<metric>"
            if len(parts) == 3:
                _, pid, metric = parts
                grouped.setdefault(pid, {})[metric] = value
        return grouped
    except Exception:
        return {}


def _get_circuit_states() -> dict[str, dict]:
    """Return per-session circuit breaker states for /metrics."""
    try:
        from archolith_proxy.proxy.circuit_breaker import get_all_circuit_states

        return get_all_circuit_states()
    except Exception:
        return {}


@router.get("/metrics")
async def metrics(request: Request, admin: None = Depends(require_admin_token)) -> dict:
    active_sessions = 0
    if is_graph_ready():
        try:
            from archolith_proxy.graph.session import list_active_sessions

            sessions = await list_active_sessions()
            active_sessions = len(sessions)
        except Exception:
            pass

    # Derived rates
    total_extractions = (
        get_metrics()["extraction_successes"]
        + get_metrics()["extraction_failures"]
        + get_metrics()["extraction_empties"]
    )
    extraction_success_rate = (
        round(get_metrics()["extraction_successes"] / total_extractions, 4)
        if total_extractions > 0
        else 0.0
    )
    avg_token_savings = (
        round(get_metrics()["token_savings_estimated"] / get_metrics()["total_requests"])
        if get_metrics()["total_requests"] > 0
        else 0
    )
    total_input = get_metrics()["total_input_tokens_seen"]
    total_savings = get_metrics()["token_savings_estimated"]
    token_savings_rate = (
        round(total_savings / total_input, 4) if total_input > 0 else 0.0
    )

    # Per-session user turn counts + cost aggregation from the trace store.
    # D10: compute all trace-derived metrics under a SINGLE lock acquisition so
    # the derived ratios come from one consistent snapshot. Previously the two
    # reads were locked separately, so a mutation in between could mix counts
    # from different states and yield inconsistent ratios.
    trace_store = getattr(request.app.state, "trace_store", get_trace_store())
    settings = get_settings()
    user_turns_by_session: dict[str, int] = {}
    total_output_tokens = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0
    curator_latencies: list[float] = []
    total_curator_tool_calls = 0
    try:
        async with trace_store._lock:
            for session_id, turns in trace_store.by_session.items():
                if not turns:
                    continue
                user_turns_by_session[session_id] = max(
                    (t.user_turn_count for t in turns), default=0
                )
                for t in turns:
                    if t.output_tokens:
                        total_output_tokens += t.output_tokens
                    total_cache_hit_tokens += t.cache_hit_tokens
                    total_cache_miss_tokens += t.cache_miss_tokens
                    if t.assembly_mode == "curator":
                        curator_latencies.append(t.assembly_latency_ms)
                        if t.curator_tool_log:
                            total_curator_tool_calls += len(t.curator_tool_log)
    except Exception:
        pass
    output_cost = total_output_tokens * settings.pricing_output_per_million / 1_000_000

    has_cache_data = total_cache_hit_tokens > 0 or total_cache_miss_tokens > 0
    if has_cache_data:
        input_cost = (
            total_cache_hit_tokens * settings.pricing_input_cached_per_million / 1_000_000
            + total_cache_miss_tokens * settings.pricing_input_per_million / 1_000_000
        )
    else:
        input_cost = total_input * settings.pricing_input_per_million / 1_000_000
    savings_cost = total_savings * settings.pricing_input_per_million / 1_000_000

    # Derived curator stats
    curator_calls = get_metrics()["curator_calls"]
    curator_timeouts = get_metrics()["curator_timeouts"]
    curator_fallbacks = get_metrics()["curator_fallbacks"]
    curator_successes = max(0, curator_calls - curator_timeouts - curator_fallbacks)
    curator_success_rate = (
        round(curator_successes / curator_calls, 4) if curator_calls > 0 else 0.0
    )

    # Derived file cache stats
    cache_hits = get_metrics()["native_read_cache_hits"]
    cache_misses = get_metrics()["native_read_cache_misses"]
    cache_total = cache_hits + cache_misses
    file_cache_hit_rate = (
        round(cache_hits / cache_total, 4) if cache_total > 0 else 0.0
    )

    # Average assembly latency (curator turns only). Collected above in the
    # single trace-store snapshot (D10).
    avg_curator_latency_ms = (
        round(sum(curator_latencies) / len(curator_latencies), 1)
        if curator_latencies
        else 0.0
    )
    avg_curator_tool_calls = (
        round(total_curator_tool_calls / len(curator_latencies), 1)
        if curator_latencies
        else 0.0
    )

    # Phase 0 — event-driven curator-worker diagnosis: quantify prepper
    # starvation (skipped on tool-call turns), cancellation (killed by the next
    # turn), the hot-path LLM-call rate, and briefing staleness (ledger_lag proxy).
    prepper_fires = get_metrics()["prepper_fires"]
    prepper_starved = get_metrics()["prepper_starved"]
    prepper_cancels = get_metrics()["prepper_cancels"]
    _prepper_boundaries = prepper_fires + prepper_starved
    prepper_starved_rate = (
        round(prepper_starved / _prepper_boundaries, 4) if _prepper_boundaries > 0 else 0.0
    )
    hot_path_llm_calls = get_metrics()["hot_path_llm_calls"]
    total_requests = get_metrics()["total_requests"]
    hot_path_llm_call_rate = (
        round(hot_path_llm_calls / total_requests, 4) if total_requests > 0 else 0.0
    )
    _lag_count = get_metrics()["hot_path_briefing_lag_count"]
    avg_briefing_lag_turns = (
        round(get_metrics()["hot_path_briefing_lag_sum"] / _lag_count, 2)
        if _lag_count > 0 else 0.0
    )
    curator_phase_latency = get_curator_phase_latency_percentiles()

    return {
        "proxy": "archolith-proxy",
        "version": __version__,
        "graph_ready": is_graph_ready(),
        "total_requests": get_metrics()["total_requests"],
        "assembly_modes": dict(get_metrics()["assembly_modes"]),
        "user_turns_by_session": user_turns_by_session,
        "extraction_successes": get_metrics()["extraction_successes"],
        "extraction_empties": get_metrics()["extraction_empties"],
        "extraction_failures": get_metrics()["extraction_failures"],
        "extraction_success_rate": extraction_success_rate,
        "upstream_errors": get_metrics()["upstream_errors"],
        "graph_errors": get_metrics()["neo4j_errors"],
        "active_sessions": active_sessions,
        "token_savings_estimated": get_metrics()["token_savings_estimated"],
        "avg_token_savings_per_request": avg_token_savings,
        "token_savings_rate": token_savings_rate,
        "total_input_tokens_seen": get_metrics()["total_input_tokens_seen"],
        "curator_calls": curator_calls,
        "curator_timeouts": curator_timeouts,
        "curator_fallbacks": curator_fallbacks,
        "curator_successes": curator_successes,
        "curator_success_rate": curator_success_rate,
        "avg_curator_latency_ms": avg_curator_latency_ms,
        "avg_curator_tool_calls": avg_curator_tool_calls,
        "curator_phase_latency_ms_p50_by_phase": {
            phase: values["p50"] for phase, values in curator_phase_latency.items()
        },
        "curator_phase_latency_ms_p95_by_phase": {
            phase: values["p95"] for phase, values in curator_phase_latency.items()
        },
        "curator_phase_latency_ms_p99_by_phase": {
            phase: values["p99"] for phase, values in curator_phase_latency.items()
        },
        "curator_phase_latency_samples_by_phase": {
            phase: values["count"] for phase, values in curator_phase_latency.items()
        },
        "curator_worker_diag": {
            "prepper_fires": prepper_fires,
            "prepper_starved": prepper_starved,
            "prepper_starved_rate": prepper_starved_rate,
            "prepper_cancels": prepper_cancels,
            "hot_path_llm_calls": hot_path_llm_calls,
            "hot_path_llm_call_rate": hot_path_llm_call_rate,
            "briefing_reads": _lag_count,
            "avg_briefing_lag_turns": avg_briefing_lag_turns,
            "deterministic_assemblies": get_metrics()["deterministic_assemblies"],
            "prepper_block_topups": get_metrics()["prepper_block_topups"],
            "prepper_block_timeouts": get_metrics()["prepper_block_timeouts"],
            "curator_workingset_evictions": get_metrics()["curator_workingset_evictions"],
        },
        "background_pass_successes": get_metrics()["background_pass_successes"],
        # Helper-LLM token totals (cumulative) — recorded into _metrics but
        # previously not surfaced here, so cost/activity was invisible. The
        # metered helper spend (extractor + curator + embeddings) lands here.
        "helper_tokens": {
            "extractor_prompt_tokens": get_metrics()["extractor_prompt_tokens_total"],
            "extractor_completion_tokens": get_metrics()["extractor_completion_tokens_total"],
            "extractor_cached_tokens": get_metrics()["extractor_cached_tokens_total"],
            "curator_prompt_tokens": get_metrics()["curator_prompt_tokens_total"],
            "curator_completion_tokens": get_metrics()["curator_completion_tokens_total"],
            "curator_cached_tokens": get_metrics()["curator_cached_tokens_total"],
            "embedding_tokens": get_metrics()["embedding_tokens_total"],
        },
        "synthetic_tool_successes": get_metrics()["synthetic_tool_successes"],
        "synthetic_tool_failures": get_metrics()["synthetic_tool_failures"],
        "synthetic_circuit_opens": get_metrics()["synthetic_circuit_opens"],
        "synthetic_circuit_hard_disables": get_metrics()["synthetic_circuit_hard_disables"],
        "synthetic_injections_skipped": get_metrics()["synthetic_injections_skipped"],
        "synthetic_circuit_states": _get_circuit_states(),
        "native_read_cache_hits": get_metrics()["native_read_cache_hits"],
        "native_read_cache_misses": get_metrics()["native_read_cache_misses"],
        "native_read_intercept_errors": get_metrics()["native_read_intercept_errors"],
        "file_cache_invalidations": get_metrics()["file_cache_invalidations"],
        "reconciled_set_size": get_metrics()["reconciled_set_size"],
        "file_cache_hit_rate": file_cache_hit_rate,
        "trace_records": trace_store.total_traces,
        "trace_sessions": trace_store.session_count,
        "uptime_s": round(time.time() - get_metrics()["start_time"], 0)
        if get_metrics()["start_time"]
        else 0,
        "total_output_tokens": total_output_tokens,
        "total_cache_hit_tokens": total_cache_hit_tokens,
        "total_cache_miss_tokens": total_cache_miss_tokens,
        "cache_hit_rate_upstream": (
            round(
                total_cache_hit_tokens
                / (total_cache_hit_tokens + total_cache_miss_tokens),
                4,
            )
            if (total_cache_hit_tokens + total_cache_miss_tokens) > 0
            else 0.0
        ),
        "cost_input": round(input_cost, 4),
        "cost_output": round(output_cost, 4),
        "cost_total": round(input_cost + output_cost, 4),
        "cost_savings": round(savings_cost, 4),
        "pricing": {
            "input_per_million": settings.pricing_input_per_million,
            "input_cached_per_million": settings.pricing_input_cached_per_million,
            "output_per_million": settings.pricing_output_per_million,
        },
        "plugins": _get_plugin_metrics(),
    }
