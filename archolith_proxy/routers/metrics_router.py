"""Metrics endpoint — process-level counters and derived stats."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from archolith_proxy.config import get_settings
from archolith_proxy.graph.backend import is_graph_ready
from archolith_proxy.metrics import get_metrics
from archolith_proxy.trace.store import get_trace_store

router = APIRouter()


def _get_circuit_states() -> dict[str, dict]:
    """Return per-session circuit breaker states for /metrics."""
    try:
        from archolith_proxy.proxy.circuit_breaker import get_all_circuit_states

        return get_all_circuit_states()
    except Exception:
        return {}


@router.get("/metrics")
async def metrics(request: Request) -> dict:
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

    # Per-session user turn counts from trace store (cold_start gate progress)
    trace_store = getattr(request.app.state, "trace_store", get_trace_store())
    user_turns_by_session: dict[str, int] = {}
    try:
        for session_id, turns in trace_store.by_session.items():
            if turns:
                user_turns_by_session[session_id] = max(
                    (t.user_turn_count for t in turns), default=0
                )
    except Exception:
        pass

    # Cost estimation from pricing config
    settings = get_settings()
    total_output_tokens = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0
    try:
        for turns in trace_store.by_session.values():
            for t in turns:
                if t.output_tokens:
                    total_output_tokens += t.output_tokens
                total_cache_hit_tokens += t.cache_hit_tokens
                total_cache_miss_tokens += t.cache_miss_tokens
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

    # Average assembly latency from trace store (curator turns only)
    curator_latencies = []
    total_curator_tool_calls = 0
    try:
        for turns in trace_store.by_session.values():
            for t in turns:
                if t.assembly_mode == "curator":
                    curator_latencies.append(t.assembly_latency_ms)
                    if t.curator_tool_log:
                        total_curator_tool_calls += len(t.curator_tool_log)
    except Exception:
        pass
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

    return {
        "proxy": "archolith-proxy",
        "version": "0.1.0",
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
        "compaction_applied": get_metrics()["compaction_applied"],
        "curator_calls": curator_calls,
        "curator_timeouts": curator_timeouts,
        "curator_fallbacks": curator_fallbacks,
        "curator_successes": curator_successes,
        "curator_success_rate": curator_success_rate,
        "avg_curator_latency_ms": avg_curator_latency_ms,
        "avg_curator_tool_calls": avg_curator_tool_calls,
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
    }
