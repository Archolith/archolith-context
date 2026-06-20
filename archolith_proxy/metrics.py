"""Process-level metrics singleton — extracted from archolith_proxy/main.py.

Provides a shared metrics dictionary and accessors, eliminating the
circular import pattern (from archolith_proxy.main import _metrics) that was
previously used in chat.py, proxy/ modules, and assembler/ modules.
"""

from __future__ import annotations

import time

_CURATOR_PHASE_LATENCY_MAX_SAMPLES = 1000
_GAUGE_METRICS = {"reconciled_set_size"}

# Module-level metrics dictionary — single source of truth for the process
_metrics: dict = {
    "start_time": 0.0,
    "total_requests": 0,
    "assembly_modes": {
        "cold_start": 0,
        "graph": 0,
        "curator": 0,
        "fallback": 0,
        "passthrough": 0,
        "agent_solo": 0,
        "agent_solo_compressed": 0,
        "briefing": 0,
        "briefing_stale": 0,
        "skipped_low_tokens": 0,
        "skipped_low_savings": 0,
        "skipped_inflation": 0,
    },
    "extraction_successes": 0,
    "extraction_empties": 0,
    "extraction_failures": 0,
    "upstream_errors": 0,
    "neo4j_errors": 0,
    "token_savings_estimated": 0,
    "total_input_tokens_seen": 0,
    "total_input_tokens_structural": 0,
    "total_input_tokens_client_reported": 0,
    # Which estimate source the assembly gate decided on (per GateSource value).
    "gate_decisions_structural_estimate": 0,
    "gate_decisions_content_estimate": 0,
    "gate_decisions_client_reported": 0,
    "gate_decisions_max_structural_client": 0,
    "promotions_attempted": 0,
    "promotions_succeeded": 0,
    "promotions_failed": 0,
    "promotions_skipped": 0,
    "proxy_recall_injections": 0,
    "background_pass_successes": 0,
    "curator_calls": 0,
    "curator_timeouts": 0,
    "curator_fallbacks": 0,
    # Phase 0 — event-driven curator-worker diagnosis (prepper starvation/cancellation).
    # prepper_fires:   a background pass was actually scheduled for a turn.
    # prepper_starved: a turn boundary where the prepper was skipped because the
    #                  turn was a tool-call continuation / non-user turn.
    # prepper_cancels: an in-flight background pass was cancelled by the next turn.
    # hot_path_llm_calls: inline curate_context made an LLM call on the request path.
    # hot_path_briefing_lag_{sum,count}: staleness (turn - briefing.source_turn) at
    #                  each hot-path briefing read; mean = sum / count.
    "prepper_fires": 0,
    "prepper_starved": 0,
    "prepper_cancels": 0,
    "hot_path_llm_calls": 0,
    "hot_path_briefing_lag_sum": 0,
    "hot_path_briefing_lag_count": 0,
    # Phase 2 — deterministic LLM-free hot-path reads (count of inline reads
    # served from the briefing in pure code, no LLM call).
    "deterministic_assemblies": 0,
    # Synchronous prepper top-up: a user turn that blocked on a prepper pass and
    # then served from the fresh briefing (topups) vs. blocked but timed out.
    "prepper_block_topups": 0,
    "prepper_block_timeouts": 0,
    # Single-leader worker leasing: this process held the curator-worker lease
    # (held) vs. another process is leader so workers are skipped here (blocked).
    "curator_worker_lease_held": 0,
    "curator_worker_lease_blocked": 0,
    # Phase latency histograms for curator profiling. Values are milliseconds,
    # grouped by phase name and bounded per phase to keep process memory stable.
    "curator_phase_latency_ms": {},
    # Phase 4 — ARC working set evicted a session's cached state under memory
    # pressure (bound exceeded). The persisted row (if any) is kept for warm-start.
    "curator_workingset_evictions": 0,
    # Helper-LLM token usage counters (cumulative, for cost telemetry)
    "extractor_prompt_tokens_total": 0,
    "extractor_completion_tokens_total": 0,
    "extractor_cached_tokens_total": 0,
    "curator_prompt_tokens_total": 0,
    "curator_completion_tokens_total": 0,
    "curator_cached_tokens_total": 0,
    "embedding_tokens_total": 0,
    "synthetic_circuit_opens": 0,
    "synthetic_circuit_hard_disables": 0,
    "synthetic_tool_failures": 0,
    "synthetic_tool_successes": 0,
    "synthetic_injections_skipped": 0,
    "native_read_cache_hits": 0,
    "native_read_cache_misses": 0,
    "native_read_intercept_errors": 0,
    "file_cache_invalidations": 0,
    "reconciled_set_size": 0,
    # Plugin metrics — aggregated from PluginRegistry.aggregate_metrics()
    # at each /metrics poll. Stored as a nested dict keyed by plugin ID.
    "plugins": {},
}


def get_metrics() -> dict:
    """Return the shared metrics dictionary (process-level)."""
    return _metrics


def record_assembly_mode(mode: str) -> None:
    """Record assembly mode in process-level metrics."""
    if mode in _metrics["assembly_modes"]:
        _metrics["assembly_modes"][mode] += 1


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return round(ordered[index], 2)


def get_curator_phase_latency_percentiles() -> dict[str, dict[str, float | int]]:
    """Return p50/p95/p99 latency summaries for curator phases."""
    raw = _metrics.get("curator_phase_latency_ms", {})
    if not isinstance(raw, dict):
        return {}
    summary: dict[str, dict[str, float | int]] = {}
    for phase, values in raw.items():
        if not isinstance(values, list):
            continue
        samples = [float(v) for v in values]
        summary[str(phase)] = {
            "count": len(samples),
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
        }
    return summary


def record_metric(key: str, delta: int | float = 1, *, phase: str | None = None) -> None:
    """Increment a numeric metric by delta."""
    if key == "curator_phase_latency_ms":
        if not phase:
            import structlog
            logger = structlog.get_logger()
            logger.warning("missing_curator_phase_latency_phase")
            return
        raw = _metrics.setdefault(key, {})
        if not isinstance(raw, dict):
            return
        samples = raw.setdefault(phase, [])
        if not isinstance(samples, list):
            return
        samples.append(float(delta))
        excess = len(samples) - _CURATOR_PHASE_LATENCY_MAX_SAMPLES
        if excess > 0:
            del samples[:excess]
        return

    if key in _metrics:
        if key in _GAUGE_METRICS:
            _metrics[key] = delta
            return
        current = _metrics[key]
        if isinstance(current, (int, float)):
            _metrics[key] = current + delta
    else:
        # Warn on unregistered metric key (likely a typo or missing registration)
        import structlog
        logger = structlog.get_logger()
        logger.warning("unregistered_metric_key", key=key)


def record_start_time() -> None:
    """Record process start time (called once in lifespan)."""
    _metrics["start_time"] = time.time()
