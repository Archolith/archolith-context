"""Tests for curator phase latency metrics."""

from __future__ import annotations

from archolith_proxy.metrics import get_curator_phase_latency_percentiles, get_metrics, record_metric


def test_curator_phase_latency_percentiles_by_phase() -> None:
    metrics = get_metrics()
    old = metrics["curator_phase_latency_ms"]
    metrics["curator_phase_latency_ms"] = {}
    try:
        for value in (10, 20, 30):
            record_metric("curator_phase_latency_ms", value, phase="llm_call")
        record_metric("curator_phase_latency_ms", 5, phase="tool_call_fetch")

        summary = get_curator_phase_latency_percentiles()

        assert summary["llm_call"] == {"count": 3, "p50": 20.0, "p95": 30.0, "p99": 30.0}
        assert summary["tool_call_fetch"] == {"count": 1, "p50": 5.0, "p95": 5.0, "p99": 5.0}
    finally:
        metrics["curator_phase_latency_ms"] = old
