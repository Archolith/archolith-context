"""Tests for the bounded per-turn extraction LLM budget."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.budget import ExtractionBudget, LLMBudgetExceeded, reserve_llm_call
from archolith_proxy.extractor.registry import ToolExtractorRegistry


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


@pytest.mark.asyncio
async def test_turn_level_capacity_is_reserved_before_per_tool_fanout():
    """Tool fallbacks cannot consume the call reserved for typed turn state."""
    from archolith_proxy.extractor.client import extract_facts_per_tool

    class BoundedLlmExtractor(ToolExtractor):
        tool_names = ("Bounded",)
        may_use_llm = True

        async def extract(self, record, http_client, turn_number, session_goal):
            try:
                reserve_llm_call(1000)
            except LLMBudgetExceeded:
                return PartialExtractionResult(source_tool="Bounded")
            return PartialExtractionResult(
                source_tool="Bounded",
                facts=[{"content": record.tool_call_id, "fact_type": "observation", "confidence": 1.0}],
                used_llm=True,
                usage={"llm_calls": 1},
            )

    registry = ToolExtractorRegistry()
    registry.register(BoundedLlmExtractor())
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({
            "facts": [], "decisions": [], "files_touched": [], "invalidated": [],
        })}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    settings = MagicMock(
        extractor_model="test", extractor_base_url="https://example.test/v1", extractor_api_key="test",
        extractor_llm_concurrency=4, extractor_llm_max_calls_per_turn=4,
        extractor_llm_max_requested_tokens_per_turn=5000,
    )

    with patch("archolith_proxy.extractor.client.get_settings", return_value=settings):
        result = await extract_facts_per_tool(
            http_client=client,
            turn_number=1,
            user_message="test",
            assistant_response="test",
            tool_records=[ToolCallRecord(str(i), "Bounded", {}, "") for i in range(4)],
            registry=registry,
        )

    assert result is not None
    assert len(result.facts) == 3
    client.post.assert_awaited_once()
