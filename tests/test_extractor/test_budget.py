"""Tests for the bounded per-turn extraction LLM budget."""

from archolith_proxy.extractor.budget import ExtractionBudget


def test_budget_limits_calls_and_requested_tokens():
    budget = ExtractionBudget(max_llm_calls=2, max_requested_tokens=3000)
    assert budget.reserve(1000)
    assert budget.reserve(2000)
    assert budget.llm_calls == 2
    assert budget.requested_tokens == 3000
    assert not budget.reserve(1)


def test_budget_rejects_request_over_token_cap_without_mutating_counters():
    budget = ExtractionBudget(max_llm_calls=4, max_requested_tokens=1000)
    assert not budget.reserve(1001)
    assert budget.llm_calls == 0
    assert budget.requested_tokens == 0
