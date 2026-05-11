"""Unit tests for context-overflow compaction."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.assembler.compaction import compact_context
from src.config import get_settings, reset_settings


class TestCompactContext:
    @pytest.mark.asyncio
    async def test_compaction_returns_shorter_text(self):
        """When the LLM returns a valid summary, it should be returned."""
        settings = get_settings()
        # Ensure API key is set (compaction skips without it)
        original_key = settings.embedding_api_key
        settings.embedding_api_key = "test-key"

        try:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {
                "choices": [
                    {
                        "message": {
                            "content": "=== SESSION OVERVIEW ===\nGoal: Build API\n=== RELEVANT CONTEXT ===\n- [error|t3] ImportError"
                        }
                    }
                ]
            }

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)

            original = "A" * 5000  # Long context
            result = await compact_context(mock_client, original, target_tokens=200)
            assert result is not None
            assert len(result) < len(original)
            assert "SESSION OVERVIEW" in result
        finally:
            settings.embedding_api_key = original_key
            reset_settings()

    @pytest.mark.asyncio
    async def test_compaction_returns_none_on_no_api_key(self):
        """When no API key, compaction should skip and return None."""
        settings = get_settings()
        original_key = settings.embedding_api_key
        settings.embedding_api_key = ""

        try:
            mock_client = AsyncMock()
            result = await compact_context(mock_client, "some context", target_tokens=200)
            assert result is None
        finally:
            settings.embedding_api_key = original_key
            reset_settings()

    @pytest.mark.asyncio
    async def test_compaction_returns_none_on_api_failure(self):
        """When the LLM API fails, compaction should return None."""
        settings = get_settings()
        original_key = settings.embedding_api_key
        settings.embedding_api_key = "test-key"

        try:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("API error"))

            result = await compact_context(mock_client, "some context", target_tokens=200)
            assert result is None
        finally:
            settings.embedding_api_key = original_key
            reset_settings()

    @pytest.mark.asyncio
    async def test_compaction_returns_none_on_empty_response(self):
        """When the LLM returns empty content, compaction should return None."""
        settings = get_settings()
        original_key = settings.embedding_api_key
        settings.embedding_api_key = "test-key"

        try:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "   "}}]
            }

            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)

            result = await compact_context(mock_client, "some context", target_tokens=200)
            assert result is None
        finally:
            settings.embedding_api_key = original_key
            reset_settings()


class TestCompactionConfig:
    def test_compaction_enabled_defaults_false(self):
        reset_settings()
        settings = get_settings()
        assert settings.compaction_enabled is False
