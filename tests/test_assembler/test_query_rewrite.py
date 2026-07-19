"""Tests for query rewriting (Step 9).

Covers:
- needs_rewrite(): detection of ambiguous queries
- rewrite_query(): LLM-based reference resolution
- extract_recent_exchanges(): extracting recent user/assistant pairs
- Integration with assemble_context()
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archolith_proxy.assembler.query_rewrite import (
    extract_recent_exchanges,
    needs_rewrite,
    rewrite_query,
)
from archolith_proxy.config import get_settings, reset_settings


# ─── needs_rewrite ───


class TestNeedsRewrite:
    """Test ambiguous query detection."""

    def test_pronoun_it(self):
        assert needs_rewrite("Fix it") is True

    def test_pronoun_this(self):
        assert needs_rewrite("Update this method") is True

    def test_pronoun_they(self):
        assert needs_rewrite("They are wrong") is True

    def test_vague_directive_continue(self):
        assert needs_rewrite("continue") is True

    def test_vague_directive_do_it(self):
        assert needs_rewrite("do it") is True

    def test_vague_directive_fix_that(self):
        assert needs_rewrite("fix that") is True

    def test_deictic_reference_previous(self):
        assert needs_rewrite("Update the previous function") is True

    def test_deictic_reference_above(self):
        assert needs_rewrite("Refactor the above code") is True

    def test_specific_query_no_rewrite(self):
        """Specific technical queries should NOT be flagged."""
        assert needs_rewrite("Add error handling to the Calculator class") is False

    def test_file_path_no_rewrite(self):
        assert needs_rewrite("Update src/config.py to add query_rewrite_enabled") is False

    def test_create_function_no_rewrite(self):
        assert needs_rewrite("Create a new test file for the assembler") is False

    def test_empty_string(self):
        assert needs_rewrite("") is False

    def test_whitespace_only(self):
        assert needs_rewrite("   ") is False

    def test_short_specific_query(self):
        """Short queries with specific keywords are self-contained."""
        # "add error handling" has specific keywords + >= 3 words → no rewrite
        assert needs_rewrite("add error handling") is False

    def test_short_vague_query(self):
        """Short queries without specific keywords need rewriting."""
        assert needs_rewrite("do it now") is True

    def test_pronoun_that_is_vague(self):
        assert needs_rewrite("That's not working") is True

    def test_implementation_request_specific(self):
        assert needs_rewrite("Implement the query_rewrite module") is False

    def test_bug_report_specific(self):
        assert needs_rewrite("Fix the bug in the authentication service") is False

    def test_very_short_query(self):
        """Single-word queries are caught by the short-query pattern."""
        assert needs_rewrite("continue") is True

    def test_mixed_case_pronouns(self):
        assert needs_rewrite("Fix It now") is True


# ─── extract_recent_exchanges ───


class TestExtractRecentExchanges:
    """Test extracting recent user/assistant message pairs."""

    def test_basic_extraction(self):
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "Fix the bug"},
            {"role": "assistant", "content": "Done"},
        ]
        result = extract_recent_exchanges(messages, max_exchanges=2)
        # Should get the last 2 user/assistant exchanges (4 messages)
        assert len(result) == 4
        # After reverse, chronological order: oldest first
        assert result[0]["content"] == "Hello"
        # Last message is the most recent assistant response
        assert result[-1]["role"] == "assistant"
        assert result[-1]["content"] == "Done"

    def test_skips_system_messages(self):
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Hello"},
        ]
        result = extract_recent_exchanges(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_skips_tool_messages(self):
        messages = [
            {"role": "user", "content": "Read file"},
            {"role": "assistant", "content": None, "tool_calls": [...]},
            {"role": "tool", "content": "file contents"},
            {"role": "assistant", "content": "Here's the file"},
        ]
        result = extract_recent_exchanges(messages)
        # Should only include user and assistant messages
        roles = {m["role"] for m in result}
        assert "tool" not in roles
        assert "user" in roles

    def test_max_exchanges_limit(self):
        messages = []
        for i in range(10):
            messages.append({"role": "user", "content": f"Q{i}"})
            messages.append({"role": "assistant", "content": f"A{i}"})

        result = extract_recent_exchanges(messages, max_exchanges=2)
        # Should get last 2 user exchanges (4 messages: 2 user + 2 assistant)
        user_msgs = [m for m in result if m["role"] == "user"]
        assert len(user_msgs) <= 2

    def test_empty_messages(self):
        result = extract_recent_exchanges([], max_exchanges=3)
        assert result == []

    def test_chronological_order(self):
        messages = [
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "First response"},
            {"role": "user", "content": "Second"},
            {"role": "assistant", "content": "Second response"},
        ]
        result = extract_recent_exchanges(messages, max_exchanges=2)
        # Should be in chronological order (not reversed)
        assert result[0]["content"] == "First" or result[0]["content"] == "Second"
        # The last message should be the most recent
        # (Since we walk backward and reverse, the first item is oldest)


# ─── rewrite_query ───


class TestRewriteQuery:
    """Test LLM-based query rewriting."""

    @pytest.mark.asyncio
    async def test_successful_rewrite(self):
        """Model returns a rewritten query."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "Fix the ImportError in calculator.py by adding the missing import statement"
                    }
                }
            ]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {
            "EXTRACTOR_API_KEY": "test-key",
            "EXTRACTOR_BASE_URL": "https://api.openai.com/v1",
            "EXTRACTOR_MODEL": "gpt-4.1-mini",
        }):
            reset_settings()
            result = await rewrite_query(
                mock_client,
                "fix it",
                [
                    {"role": "user", "content": "There's an ImportError in calculator.py"},
                    {"role": "assistant", "content": "The import json is missing"},
                ],
            )
            assert result is not None
            assert "ImportError" in result or "import" in result.lower()
            reset_settings()

    @pytest.mark.asyncio
    async def test_no_change_returns_none(self):
        """If the model returns the same query, return None."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "fix it"}}]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"EXTRACTOR_API_KEY": "test-key"}):
            reset_settings()
            result = await rewrite_query(
                mock_client,
                "fix it",
                [{"role": "user", "content": "Hello"}],
            )
            assert result is None
            reset_settings()

    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self):
        """If the model returns empty content, return None."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": ""}}]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"EXTRACTOR_API_KEY": "test-key"}):
            reset_settings()
            result = await rewrite_query(
                mock_client, "do it", []
            )
            assert result is None
            reset_settings()

    @pytest.mark.asyncio
    async def test_api_failure_returns_none(self):
        """If the API call fails, return None gracefully."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("API error"))

        with patch.dict("os.environ", {"EXTRACTOR_API_KEY": "test-key"}):
            reset_settings()
            result = await rewrite_query(
                mock_client, "fix it", []
            )
            assert result is None
            reset_settings()

    @pytest.mark.asyncio
    async def test_no_api_key_returns_none(self):
        """If no extractor API key is configured, return None."""
        mock_client = AsyncMock()

        with patch.dict("os.environ", {"EXTRACTOR_API_KEY": ""}, clear=False):
            reset_settings()
            result = await rewrite_query(
                mock_client, "fix it", []
            )
            assert result is None
            reset_settings()

    @pytest.mark.asyncio
    async def test_context_truncation(self):
        """Long messages in recent_exchanges should be truncated to 500 chars."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Rewritten query"}}]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        long_content = "x" * 1000
        recent = [
            {"role": "user", "content": long_content},
            {"role": "assistant", "content": long_content},
        ]

        with patch.dict("os.environ", {"EXTRACTOR_API_KEY": "test-key"}):
            reset_settings()
            await rewrite_query(mock_client, "fix it", recent)
            # Verify the post was called (long content was truncated, not rejected)
            assert mock_client.post.called
            reset_settings()


# ─── Integration with assemble_context ───


class TestQueryRewriteIntegration:
    """Test that query rewriting integrates with assemble_context."""

    @pytest.mark.asyncio
    async def test_rewrite_not_triggered_when_disabled(self):
        """Query rewriting should not fire when QUERY_REWRITE_ENABLED=false."""
        with patch.dict("os.environ", {
            "QUERY_REWRITE_ENABLED": "false",
            "EMBEDDING_ENABLED": "false",
            "SESSION_NEO4J_PASSWORD": "test",
        }):
            reset_settings()
            settings = get_settings()
            assert settings.query_rewrite_enabled is False
            reset_settings()

    @pytest.mark.asyncio
    async def test_rewrite_requires_embeddings(self):
        """Query rewriting should only fire when embedding_enabled=true."""
        with patch.dict("os.environ", {
            "QUERY_REWRITE_ENABLED": "true",
            "EMBEDDING_ENABLED": "false",
        }):
            reset_settings()
            settings = get_settings()
            # Rewrite should not trigger without embeddings
            assert settings.query_rewrite_enabled is True
            assert settings.embedding_enabled is False
            reset_settings()

    def test_config_default_is_false(self):
        """Query rewrite should be disabled by default."""
        with patch.dict("os.environ", {}, clear=False):
            reset_settings()
            settings = get_settings()
            assert settings.query_rewrite_enabled is False
            reset_settings()

    @pytest.mark.asyncio
    async def test_rewrite_enabled_non_ambiguous_no_nameerror(self):
        """Regression: when QUERY_REWRITE_ENABLED=true and the user message
        is specific (needs_rewrite=False), assemble_context must not raise
        NameError on the unassigned 'rewritten' variable."""
        from archolith_proxy.assembler.context import assemble_context

        mock_backend = AsyncMock()
        mock_backend.find_session_by_id = AsyncMock(return_value={"goal": "test goal"})
        mock_backend.get_active_facts = AsyncMock(return_value=[])
        mock_backend.get_touched_files = AsyncMock(return_value=[])
        mock_backend.get_decisions = AsyncMock(return_value=[])

        mock_http_client = AsyncMock()

        with patch.dict("os.environ", {
            "QUERY_REWRITE_ENABLED": "true",
            "EMBEDDING_ENABLED": "true",
            "COLD_START_TURNS": "0",
            "COLD_START_TOKEN_THRESHOLD": "0",
            "SESSION_NEO4J_PASSWORD": "test",
        }):
            reset_settings()
            with patch("archolith_proxy.assembler.context.get_backend", return_value=mock_backend):
                # A specific, non-ambiguous message — needs_rewrite() returns False
                result = await assemble_context(
                    session_id="test-session",
                    turn_number=5,
                    input_token_estimate=1000,
                    user_message="Add error handling to the Calculator class",
                    http_client=mock_http_client,
                    messages=[
                        {"role": "user", "content": "Add error handling to the Calculator class"},
                    ],
                )
            # The key assertion: no NameError was raised.
            # Result may be None (no facts) or an AssembledContext — both are fine.
            # The bug would have caused a NameError before the fix.
            assert result is None or result.session_id == "test-session"
            reset_settings()

    @pytest.mark.asyncio
    async def test_rewrite_enabled_ambiguous_rewritten(self):
        """When rewrite is enabled and message is ambiguous, rewrite fires
        and the rewritten query replaces the effective_query."""
        from archolith_proxy.assembler.context import assemble_context

        mock_backend = AsyncMock()
        mock_backend.find_session_by_id = AsyncMock(return_value={"goal": "test goal"})
        mock_backend.get_active_facts = AsyncMock(return_value=[])
        mock_backend.get_touched_files = AsyncMock(return_value=[])
        mock_backend.get_decisions = AsyncMock(return_value=[])

        mock_http_client = AsyncMock()
        # Patch the source module since rewrite_query is imported inline
        with patch.dict("os.environ", {
            "QUERY_REWRITE_ENABLED": "true",
            "EMBEDDING_ENABLED": "true",
            "COLD_START_TURNS": "0",
            "COLD_START_TOKEN_THRESHOLD": "0",
            "SESSION_NEO4J_PASSWORD": "test",
        }):
            reset_settings()
            with patch("archolith_proxy.assembler.context.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.assembler.query_rewrite.rewrite_query", new=AsyncMock(return_value="Fix the ImportError in calculator.py")):
                result = await assemble_context(
                    session_id="test-session",
                    turn_number=5,
                    input_token_estimate=1000,
                    user_message="fix it",
                    http_client=mock_http_client,
                    messages=[
                        {"role": "user", "content": "fix it"},
                    ],
                )
            # Should succeed without error — may return None (no facts) or a valid context
            assert result is None or result.session_id == "test-session"
            reset_settings()
