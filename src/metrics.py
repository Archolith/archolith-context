"""Process-level metrics singleton — extracted from src/main.py.

Provides a shared metrics dictionary and accessors, eliminating the
circular import pattern (from src.main import _metrics) that was
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
        "fallback": 0,
        "passthrough": 0,
        "skipped_low_tokens": 0,
        "skipped_low_savings": 0,
    },
    "extraction_successes": 0,
    "extraction_failures": 0,
    "upstream_errors": 0,
    "neo4j_errors": 0,
    "token_savings_estimated": 0,
    "total_input_tokens_seen": 0,
    "compaction_applied": 0,
    "promotions_attempted": 0,
    "promotions_succeeded": 0,
    "promotions_failed": 0,
    "promotions_skipped": 0,
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


def record_start_time() -> None:
    """Record process start time (called once in lifespan)."""
    _metrics["start_time"] = time.time()
