"""Tests for helper-LLM prompt-cache hit token tracking (cached_tokens)."""

from __future__ import annotations

from types import SimpleNamespace

from archolith_proxy.models.dtos import TurnTrace
from archolith_proxy.trace.builder import TraceBuilder


# ---------------------------------------------------------------------------
# TurnTrace JSONL round-trip
# ---------------------------------------------------------------------------


def test_turn_trace_cached_tokens_round_trip() -> None:
    """TurnTrace serializes curator_cached_tokens and extractor_cached_tokens."""
    trace = TurnTrace(
        curator_cached_tokens=123,
        extractor_cached_tokens=456,
    )
    dumped = trace.model_dump()
    assert dumped["curator_cached_tokens"] == 123
    assert dumped["extractor_cached_tokens"] == 456


def test_turn_trace_cached_tokens_default_zero() -> None:
    """New cached_tokens fields default to 0."""
    trace = TurnTrace()
    assert trace.curator_cached_tokens == 0
    assert trace.extractor_cached_tokens == 0


# ---------------------------------------------------------------------------
# TraceBuilder.set_helper_usage writes both fields
# ---------------------------------------------------------------------------


def test_set_helper_usage_writes_cached_tokens() -> None:
    """set_helper_usage propagates extractor_cached_tokens and curator_cached_tokens."""
    builder = TraceBuilder()
    builder.set_helper_usage(
        extractor_cached_tokens=77,
        curator_cached_tokens=88,
    )
    data = builder.build()
    assert data.extractor_cached_tokens == 77
    assert data.curator_cached_tokens == 88


def test_set_helper_usage_cached_tokens_zero_not_written() -> None:
    """Zero cached_tokens values are not written (consistent with if value: pattern)."""
    builder = TraceBuilder()
    builder.set_helper_usage(
        extractor_prompt_tokens=10,
        extractor_cached_tokens=0,
        curator_cached_tokens=0,
    )
    data = builder.build()
    assert data.extractor_cached_tokens == 0
    assert data.curator_cached_tokens == 0


# ---------------------------------------------------------------------------
# Curator loop: cached_tokens accumulation
# ---------------------------------------------------------------------------


def _make_usage(prompt_tokens: int, completion_tokens: int, cached: int | None) -> SimpleNamespace:
    """Build a fake response.usage object."""
    details = SimpleNamespace(cached_tokens=cached) if cached is not None else None
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=details,
    )


def test_curator_cached_tokens_accumulates() -> None:
    """curator_cached_tokens accumulates getattr-safe cached_tokens from response.usage."""
    accumulated = 0
    for cached in [50, 30]:
        usage = _make_usage(100, 20, cached)
        accumulated += (getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
    assert accumulated == 80


def test_curator_cached_tokens_absent_field_is_zero() -> None:
    """When prompt_tokens_details is absent, cached accumulation is 0 with no crash."""
    usage = _make_usage(100, 20, None)
    result = (getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
    assert result == 0


def test_curator_cached_tokens_details_none_object() -> None:
    """When prompt_tokens_details is present but cached_tokens is None, result is 0."""
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=None),
    )
    result = (getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
    assert result == 0


# ---------------------------------------------------------------------------
# Extractor: usage dict cached_tokens
# ---------------------------------------------------------------------------


def test_extractor_usage_dict_carries_cached_tokens() -> None:
    """Extractor usage dict includes cached_tokens key when present."""
    raw_usage = {
        "prompt_tokens": 200,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 60},
    }
    parsed_usage = {
        "prompt_tokens": raw_usage.get("prompt_tokens", 0) or 0,
        "completion_tokens": raw_usage.get("completion_tokens", 0) or 0,
        "llm_calls": 1,
        "cached_tokens": (raw_usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0,
    }
    assert parsed_usage["cached_tokens"] == 60


def test_extractor_usage_dict_missing_details_zero() -> None:
    """Extractor usage dict defaults cached_tokens to 0 when prompt_tokens_details absent."""
    raw_usage = {"prompt_tokens": 200, "completion_tokens": 40}
    parsed_usage = {
        "cached_tokens": (raw_usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0,
    }
    assert parsed_usage["cached_tokens"] == 0


def test_extractor_usage_dict_null_details_zero() -> None:
    """Extractor usage dict handles None prompt_tokens_details without crash."""
    raw_usage = {"prompt_tokens": 200, "completion_tokens": 40, "prompt_tokens_details": None}
    parsed_usage = {
        "cached_tokens": (raw_usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0,
    }
    assert parsed_usage["cached_tokens"] == 0


def test_extractor_accumulator_cached_tokens() -> None:
    """Accumulator sums cached_tokens across per-tool results."""
    usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "cached_tokens": 0}
    for per_tool_usage in [{"cached_tokens": 10}, {"cached_tokens": 25}, {}]:
        usage["cached_tokens"] += per_tool_usage.get("cached_tokens", 0) or 0
    assert usage["cached_tokens"] == 35
