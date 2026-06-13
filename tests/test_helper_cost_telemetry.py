"""Tests for helper-LLM cost telemetry (extractor / curator / embedding token usage)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archolith_proxy.curator.result import CuratorResult
from archolith_proxy.models.dtos import ExtractionResult, TurnTrace
from archolith_proxy.trace.builder import TraceBuilder


# ---------------------------------------------------------------------------
# TurnTrace new fields
# ---------------------------------------------------------------------------


def test_turn_trace_has_helper_usage_fields() -> None:
    """TurnTrace serializes the six new helper-LLM fields."""
    trace = TurnTrace(
        extractor_prompt_tokens=100,
        extractor_completion_tokens=50,
        extractor_llm_calls=2,
        curator_prompt_tokens=300,
        curator_completion_tokens=200,
        embedding_tokens=500,
    )
    assert trace.extractor_prompt_tokens == 100
    assert trace.extractor_completion_tokens == 50
    assert trace.extractor_llm_calls == 2
    assert trace.curator_prompt_tokens == 300
    assert trace.curator_completion_tokens == 200
    assert trace.embedding_tokens == 500

    # Round-trip via model_dump (dict serialization)
    dumped = trace.model_dump()
    assert dumped["extractor_prompt_tokens"] == 100
    assert dumped["extractor_completion_tokens"] == 50
    assert dumped["extractor_llm_calls"] == 2
    assert dumped["curator_prompt_tokens"] == 300
    assert dumped["curator_completion_tokens"] == 200
    assert dumped["embedding_tokens"] == 500


def test_turn_trace_helper_usage_defaults_zero() -> None:
    """New helper-LLM fields default to 0 (backward compatible)."""
    trace = TurnTrace()
    assert trace.extractor_prompt_tokens == 0
    assert trace.extractor_completion_tokens == 0
    assert trace.extractor_llm_calls == 0
    assert trace.curator_prompt_tokens == 0
    assert trace.curator_completion_tokens == 0
    assert trace.embedding_tokens == 0


# ---------------------------------------------------------------------------
# TraceBuilder.set_helper_usage
# ---------------------------------------------------------------------------


def test_trace_builder_set_helper_usage() -> None:
    """set_helper_usage populates all six fields in the built trace."""
    builder = TraceBuilder()
    builder.set_helper_usage(
        extractor_prompt_tokens=100,
        extractor_completion_tokens=50,
        extractor_llm_calls=2,
        curator_prompt_tokens=300,
        curator_completion_tokens=200,
        embedding_tokens=500,
    )
    trace = builder.build()
    assert trace.extractor_prompt_tokens == 100
    assert trace.extractor_completion_tokens == 50
    assert trace.extractor_llm_calls == 2
    assert trace.curator_prompt_tokens == 300
    assert trace.curator_completion_tokens == 200
    assert trace.embedding_tokens == 500


def test_trace_builder_set_helper_usage_partial() -> None:
    """Partial set_helper_usage leaves unset fields at default 0."""
    builder = TraceBuilder()
    builder.set_helper_usage(extractor_prompt_tokens=100)
    trace = builder.build()
    assert trace.extractor_prompt_tokens == 100
    assert trace.extractor_completion_tokens == 0
    assert trace.extractor_llm_calls == 0
    assert trace.embedding_tokens == 0


def test_trace_builder_set_helper_usage_preserves_prior_fields() -> None:
    """Separate helper stages must not zero each other's usage fields."""
    builder = TraceBuilder()
    builder.set_helper_usage(curator_prompt_tokens=300, curator_completion_tokens=200)
    builder.set_helper_usage(
        extractor_prompt_tokens=100,
        extractor_completion_tokens=50,
        extractor_llm_calls=2,
        embedding_tokens=500,
    )

    trace = builder.build()
    assert trace.curator_prompt_tokens == 300
    assert trace.curator_completion_tokens == 200
    assert trace.extractor_prompt_tokens == 100
    assert trace.extractor_completion_tokens == 50
    assert trace.extractor_llm_calls == 2
    assert trace.embedding_tokens == 500


# ---------------------------------------------------------------------------
# CuratorResult new fields
# ---------------------------------------------------------------------------


def test_curator_result_has_token_usage() -> None:
    """CuratorResult has prompt_tokens_used and completion_tokens_used."""
    result = CuratorResult(
        context_text="test context",
        prompt_tokens_used=150,
        completion_tokens_used=75,
    )
    assert result.prompt_tokens_used == 150
    assert result.completion_tokens_used == 75


def test_curator_result_token_usage_defaults_zero() -> None:
    """New token fields default to 0 (backward compatible)."""
    result = CuratorResult(context_text="test")
    assert result.prompt_tokens_used == 0
    assert result.completion_tokens_used == 0


# ---------------------------------------------------------------------------
# ExtractionResult usage field
# ---------------------------------------------------------------------------


def test_extraction_result_usage_field() -> None:
    """ExtractionResult accepts optional usage dict."""
    result = ExtractionResult(
        facts=[], files_touched=[], decisions=[],
        invalidated_fact_ids=[], turn_number=1,
        usage={"prompt_tokens": 100, "completion_tokens": 50},
    )
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50}


def test_extraction_result_usage_defaults_empty() -> None:
    """usage field defaults to empty dict when not provided."""
    result = ExtractionResult(
        facts=[], files_touched=[], decisions=[],
        invalidated_fact_ids=[], turn_number=1,
    )
    assert result.usage == {}


# ---------------------------------------------------------------------------
# BackgroundPassTrace new fields
# ---------------------------------------------------------------------------


def test_background_pass_trace_has_usage() -> None:
    """BackgroundPassTrace records prompt_tokens_used and completion_tokens_used."""
    from archolith_proxy.models.dtos import BackgroundPassTrace

    bpt = BackgroundPassTrace(
        session_id="test", trigger_turn=1,
        prompt_tokens_used=100, completion_tokens_used=50,
    )
    assert bpt.prompt_tokens_used == 100
    assert bpt.completion_tokens_used == 50


def test_background_pass_trace_usage_defaults_zero() -> None:
    """New background pass token fields default to 0."""
    from archolith_proxy.models.dtos import BackgroundPassTrace

    bpt = BackgroundPassTrace(session_id="test", trigger_turn=1)
    assert bpt.prompt_tokens_used == 0
    assert bpt.completion_tokens_used == 0


# ---------------------------------------------------------------------------
# compute_embeddings_batch return type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_embeddings_batch_returns_usage() -> None:
    """compute_embeddings_batch returns (embeddings, total_tokens)."""
    from archolith_proxy.extractor.embeddings import compute_embeddings_batch

    mock_settings = MagicMock()
    mock_settings.embedding_api_key = "test-key"
    mock_settings.embedding_base_url = "https://api.openai.com/v1"
    mock_settings.embedding_model = "text-embedding-3-small"

    client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "usage": {"total_tokens": 15},
    }
    # client.post is called with await, so it must be an AsyncMock
    client.post = AsyncMock(return_value=mock_response)

    texts = ["hello world"]

    with patch("archolith_proxy.extractor.embeddings.get_settings", return_value=mock_settings):
        embeddings, total_tokens = await compute_embeddings_batch(client, texts)

    assert len(embeddings) == 1
    assert embeddings[0] == [0.1, 0.2, 0.3]
    assert total_tokens == 15


@pytest.mark.asyncio
async def test_compute_embeddings_batch_no_texts() -> None:
    """compute_embeddings_batch returns empty lists with 0 tokens when no texts."""
    from archolith_proxy.extractor.embeddings import compute_embeddings_batch

    client = AsyncMock()
    embeddings, total_tokens = await compute_embeddings_batch(client, [])

    assert embeddings == []
    assert total_tokens == 0
