"""Process-level metrics singleton — extracted from archolith_proxy/main.py.

Provides a shared metrics dictionary and accessors, eliminating the
circular import pattern (from archolith_proxy.main import _metrics) that was
previously used in chat.py, proxy/ modules, and assembler/ modules.
"""

from __future__ import annotations

import time

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
    "compaction_applied": 0,
    "promotions_attempted": 0,
    "promotions_succeeded": 0,
    "promotions_failed": 0,
    "promotions_skipped": 0,
    "proxy_recall_injections": 0,
    "background_pass_successes": 0,
    "curator_calls": 0,
    "curator_timeouts": 0,
    "curator_fallbacks": 0,
    "synthetic_circuit_opens": 0,
    "synthetic_circuit_hard_disables": 0,
    "synthetic_tool_failures": 0,
    "synthetic_tool_successes": 0,
    "synthetic_injections_skipped": 0,
    "native_read_cache_hits": 0,
    "native_read_cache_misses": 0,
    "native_read_intercept_errors": 0,
    "file_cache_invalidations": 0,
}


def get_metrics() -> dict:
    """Return the shared metrics dictionary (process-level)."""
    return _metrics


def record_assembly_mode(mode: str) -> None:
    """Record assembly mode in process-level metrics."""
    if mode in _metrics["assembly_modes"]:
        _metrics["assembly_modes"][mode] += 1


def record_metric(key: str, delta: int | float = 1) -> None:
    """Increment a numeric metric by delta."""
    if key in _metrics:
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
