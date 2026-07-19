"""Tests for the bounded per-turn extraction LLM budget."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.budget import ExtractionBudget, reset_budget, set_budget
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
        llm_requested_tokens = 1000

        async def extract(self, record, http_client, turn_number, session_goal):
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


@pytest.mark.asyncio
async def test_undeclared_llm_extractor_is_not_invoked():
    from archolith_proxy.extractor.client import _extract_with_semaphore

    class UndeclaredLlmExtractor(ToolExtractor):
        tool_names = ("Undeclared",)
        may_use_llm = True

        async def extract(self, *args, **kwargs):
            raise AssertionError("extract() must not run without an LLM token declaration")

    result = await _extract_with_semaphore(
        UndeclaredLlmExtractor(),
        ToolCallRecord("call-1", "Undeclared", {}, "raw tool output"),
        AsyncMock(),
        1,
        None,
    )

    assert result.used_llm is False
    assert result.facts[0]["content"] == "[Undeclared] raw tool output"


@pytest.mark.asyncio
async def test_budget_exhaustion_prevents_custom_extractor_http_call():
    from archolith_proxy.extractor.client import _extract_with_semaphore

    class HttpLlmExtractor(ToolExtractor):
        tool_names = ("HttpLlm",)
        may_use_llm = True
        llm_requested_tokens = 1000

        async def extract(self, record, http_client, turn_number, session_goal):
            await http_client.post("https://example.test/should-not-run")
            return PartialExtractionResult(source_tool="HttpLlm", used_llm=True)

    client = AsyncMock()
    token = set_budget(ExtractionBudget(max_llm_calls=0, max_requested_tokens=0))
    try:
        result = await _extract_with_semaphore(
            HttpLlmExtractor(),
            ToolCallRecord("call-1", "HttpLlm", {}, "raw tool output"),
            client,
            1,
            None,
        )
    finally:
        reset_budget(token)

    assert result.used_llm is False
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_is_reset_after_unexpected_fanout_failure():
    from archolith_proxy.extractor.budget import reserve_llm_call
    from archolith_proxy.extractor.client import extract_facts_per_tool

    settings = MagicMock(
        extractor_llm_max_calls_per_turn=4,
        extractor_llm_max_requested_tokens_per_turn=5000,
    )
    with (
        patch("archolith_proxy.extractor.client.get_settings", return_value=settings),
        patch("archolith_proxy.extractor.client.asyncio.gather", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await extract_facts_per_tool(
            http_client=AsyncMock(),
            turn_number=1,
            user_message="test",
            assistant_response="test",
            tool_records=[],
        )

    assert reserve_llm_call(10) is True
