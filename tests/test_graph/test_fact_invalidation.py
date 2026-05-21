"""Tests for fact invalidation — find_matching_fact_ids uses Jaccard similarity
to match extraction-model description strings to actual fact IDs."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from archolith_proxy.graph.facts import find_matching_fact_ids, _INVALIDATION_MATCH_THRESHOLD


def _make_active_facts(facts: list[tuple[str, str]]) -> list[dict]:
    """Helper to create mock active fact dicts.

    Args: list of (fact_id, content) tuples.
    """
    return [{"fact_id": fid, "content": content} for fid, content in facts]


@pytest.mark.asyncio
class TestFindMatchingFactIds:
    async def test_exact_content_match(self):
        """Description exactly matching a fact's content should find it."""
        active = _make_active_facts([
            ("abc123", "src/main.py has a missing import for json"),
            ("def456", "Auth module uses JWT tokens"),
        ])
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result = await find_matching_fact_ids("sess1", ["src/main.py has a missing import for json"])
        assert result == ["abc123"]

    async def test_near_match_above_threshold(self):
        """A description that's similar (but not identical) should match if above threshold."""
        active = _make_active_facts([
            ("abc123", "Build fails with TypeError on line 42 of api.ts"),
            ("def456", "Auth module uses JWT tokens"),
        ])
        # "Build fails with TypeError on line 42" vs full content — high overlap
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result = await find_matching_fact_ids("sess1", ["Build fails with TypeError on line 42"])
        assert "abc123" in result

    async def test_no_match_below_threshold(self):
        """A description that's too different should not match."""
        active = _make_active_facts([
            ("abc123", "src/main.py is a FastAPI application entry point"),
            ("def456", "Auth module uses JWT tokens for authentication"),
        ])
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result = await find_matching_fact_ids("sess1", ["The database migration was successful"])
        assert result == []

    async def test_empty_descriptions(self):
        """Empty description list should return empty."""
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=[]):
            result = await find_matching_fact_ids("sess1", [])
        assert result == []

    async def test_no_active_facts(self):
        """No active facts should return empty even with descriptions."""
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=[]):
            result = await find_matching_fact_ids("sess1", ["some description"])
        assert result == []

    async def test_multiple_descriptions(self):
        """Multiple descriptions should each match independently."""
        active = _make_active_facts([
            ("aaa", "Build error: Type mismatch at src/api.ts:42"),
            ("bbb", "Auth module uses JWT tokens for session management"),
            ("ccc", "Database has users, sessions, and tokens tables"),
        ])
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result = await find_matching_fact_ids("sess1", [
                "Build error: Type mismatch at src/api.ts:42",
                "Auth module uses JWT tokens for session management",
            ])
        assert set(result) == {"aaa", "bbb"}

    async def test_best_match_selected(self):
        """When a description partially matches multiple facts, pick the best one."""
        active = _make_active_facts([
            ("aaa", "The user module has a login function"),
            ("bbb", "The admin module has a login function"),
            ("ccc", "The user module has a logout function"),
        ])
        # "The user module login" should match "aaa" best
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result = await find_matching_fact_ids("sess1", ["The user module has a login function"])
        assert "aaa" in result

    async def test_custom_threshold(self):
        """Custom threshold should be respected."""
        active = _make_active_facts([
            ("aaa", "a b c d e f g h i j"),
        ])
        # "a b c d e f" has 6/10 overlap with "a b c d e f g h i j" = 0.6
        # With default threshold (0.60), it should match
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result_default = await find_matching_fact_ids("sess1", ["a b c d e f"])
        # With a very high threshold, it should not match
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result_high = await find_matching_fact_ids("sess1", ["a b c d e f"], threshold=0.95)
        assert "aaa" in result_default
        assert result_high == []

    async def test_dedup_matched_ids(self):
        """Two descriptions matching the same fact should only return one ID."""
        active = _make_active_facts([
            ("aaa", "The build error on line 42 was fixed by adding the import"),
        ])
        with patch("archolith_proxy.graph.facts.get_active_facts", new_callable=AsyncMock, return_value=active):
            result = await find_matching_fact_ids("sess1", [
                "The build error on line 42 was fixed by adding the import",
                "The build error on line 42 was fixed by adding the import",
            ])
        # Should deduplicate — only one ID
        assert len(result) == 1
        assert result[0] == "aaa"

    async def test_invalidation_threshold_constant(self):
        """Default threshold should be 0.60 (lower than dedup's 0.85)."""
        assert _INVALIDATION_MATCH_THRESHOLD == 0.60
