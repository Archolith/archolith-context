"""Tests for fact deduplication module."""

from __future__ import annotations

import pytest

from archolith_proxy.extractor.dedup import (
    DEFAULT_SIMILARITY_THRESHOLD,
    _normalize,
    _tokenize,
    deduplicate_facts,
    is_duplicate,
    jaccard_similarity,
)


# ── _normalize ──────────────────────────────────────────────────────────────

class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Hello World") == "hello world"

    def test_strip_surrounding_quotes(self):
        assert _normalize('"some fact"') == "some fact"
        assert _normalize("'some fact'") == "some fact"

    def test_collapse_whitespace(self):
        assert _normalize("a  b   c") == "a b c"

    def test_strip_trailing_punctuation(self):
        assert _normalize("some fact.") == "some fact"
        assert _normalize("some fact!") == "some fact"
        assert _normalize("some fact;") == "some fact"

    def test_combined(self):
        # Quotes stripped, whitespace collapsed, trailing punct stripped
        assert _normalize("Hello.  World!") == "hello. world"


# ── _tokenize ───────────────────────────────────────────────────────────────

class TestTokenize:
    def test_basic(self):
        tokens = _tokenize("hello world")
        assert tokens == {"hello", "world"}

    def test_empty(self):
        assert _tokenize("") == set()

    def test_dedup_tokens(self):
        tokens = _tokenize("the the the")
        assert tokens == {"the"}

    def test_normalizes_first(self):
        tokens = _tokenize("Hello World!")
        assert tokens == {"hello", "world"}


# ── jaccard_similarity ──────────────────────────────────────────────────────

class TestJaccardSimilarity:
    def test_identical(self):
        assert jaccard_similarity("the cat sat", "the cat sat") == 1.0

    def test_no_overlap(self):
        assert jaccard_similarity("alpha beta", "gamma delta") == 0.0

    def test_partial_overlap(self):
        # "a b c" vs "a b d" → intersection {a, b} = 2, union {a, b, c, d} = 4
        sim = jaccard_similarity("a b c", "a b d")
        assert abs(sim - 0.5) < 0.01

    def test_empty_string_a(self):
        assert jaccard_similarity("", "hello") == 0.0

    def test_empty_string_b(self):
        assert jaccard_similarity("hello", "") == 0.0

    def test_both_empty(self):
        assert jaccard_similarity("", "") == 0.0

    def test_case_insensitive(self):
        assert jaccard_similarity("Hello World", "hello world") == 1.0

    def test_punctuation_ignored(self):
        assert jaccard_similarity("fact one.", "fact one!") == 1.0


# ── is_duplicate ────────────────────────────────────────────────────────────

class TestIsDuplicate:
    def test_exact_duplicate(self):
        existing = [{"content": "src/main.py is a FastAPI application"}]
        assert is_duplicate("src/main.py is a FastAPI application", existing) is True

    def test_near_duplicate_above_threshold(self):
        existing = [{"content": "src/main.py is a FastAPI application entry point module"}]
        # Very similar — should be above 0.85 with enough token overlap
        # "src/main.py is a FastAPI application entry point" vs
        # "src/main.py is a FastAPI application entry point module"
        # tokens overlap is 8/9 ≈ 0.89
        assert is_duplicate(
            "src/main.py is a FastAPI application entry point", existing
        ) is True

    def test_distinct_fact_below_threshold(self):
        existing = [{"content": "src/main.py is a FastAPI application"}]
        assert is_duplicate(
            "Auth module uses JWT tokens for authentication", existing
        ) is False

    def test_custom_threshold(self):
        existing = [{"content": "a b c d e"}]
        # "a b c d f" has 4/6 overlap = 0.667
        assert is_duplicate("a b c d f", existing, threshold=0.5) is True
        assert is_duplicate("a b c d f", existing, threshold=0.9) is False

    def test_empty_existing(self):
        assert is_duplicate("any fact", []) is False

    def test_existing_with_empty_content(self):
        existing = [{"content": ""}, {"content": "something else"}]
        assert is_duplicate("something else", existing) is True

    def test_no_content_key(self):
        existing = [{"other_key": "value"}]
        assert is_duplicate("any fact", existing) is False


# ── deduplicate_facts ───────────────────────────────────────────────────────

class TestDeduplicateFacts:
    def test_all_unique(self):
        new = [
            {"content": "fact one", "fact_type": "observation"},
            {"content": "fact two", "fact_type": "tool_result"},
        ]
        existing = [{"content": "fact three"}]
        result = deduplicate_facts(new, existing)
        assert len(result) == 2

    def test_some_duplicates(self):
        new = [
            {"content": "src/app.py has 340 lines of code", "fact_type": "tool_result"},
            {"content": "src/app.py has 340 lines of code total", "fact_type": "observation"},
            {"content": "a completely different fact", "fact_type": "state"},
        ]
        existing = []
        # First two are near-duplicates of each other (Jaccard ~0.86 > 0.85), so the
        # second is dropped by within-batch dedup; the distinct fact is kept.
        result = deduplicate_facts(new, existing)
        assert len(result) == 2
        assert result[0]["fact_type"] == "tool_result"
        assert result[1]["fact_type"] == "state"

    def test_within_batch_dedup(self):
        """Near-duplicates within a single batch collapse; distinct facts survive.

        The two main.py facts have Jaccard ~0.875 (above the 0.85 threshold) so the
        second is dropped as a within-batch near-duplicate; the JWT fact is distinct
        and must be kept.
        """
        new = [
            {"content": "src/main.py is a FastAPI application entry point module", "fact_type": "observation"},
            {"content": "src/main.py is a FastAPI application entry point", "fact_type": "observation"},
            {"content": "Auth module uses JWT tokens for sessions", "fact_type": "decision"},
        ]
        existing = []
        result = deduplicate_facts(new, existing)
        # Near-duplicate collapsed (3 -> 2); first occurrence + distinct fact kept.
        assert len(result) == 2
        assert result[0]["content"] == "src/main.py is a FastAPI application entry point module"
        assert result[1]["content"] == "Auth module uses JWT tokens for sessions"

    def test_dedup_against_existing(self):
        existing = [{"content": "src/main.py is a FastAPI application entry point module"}]
        new = [
            # Near-duplicate of existing (Jaccard > 0.85)
            {"content": "src/main.py is a FastAPI application entry point", "fact_type": "observation"},
            {"content": "Auth module uses JWT tokens", "fact_type": "decision"},
        ]
        result = deduplicate_facts(new, existing)
        assert len(result) == 1
        assert result[0]["content"] == "Auth module uses JWT tokens"

    def test_empty_new(self):
        result = deduplicate_facts([], [{"content": "existing"}])
        assert result == []

    def test_empty_existing(self):
        new = [{"content": "some fact"}]
        result = deduplicate_facts(new, [])
        assert len(result) == 1

    def test_preserves_fact_metadata(self):
        new = [
            {
                "content": "unique fact here",
                "fact_type": "tool_result",
                "confidence": 0.95,
            }
        ]
        existing = [{"content": "unrelated"}]
        result = deduplicate_facts(new, existing)
        assert len(result) == 1
        assert result[0]["fact_type"] == "tool_result"
        assert result[0]["confidence"] == 0.95

    def test_high_similarity_threshold(self):
        existing = [{"content": "a b c d e f g"}]
        new = [{"content": "a b c d e f h"}]
        # Very similar but with custom high threshold, should not be duplicate
        result = deduplicate_facts(new, existing, threshold=0.99)
        assert len(result) == 1

    def test_low_similarity_threshold(self):
        existing = [{"content": "a b c d"}]
        new = [{"content": "a b x y"}]
        # 50% overlap — with low threshold, it's a dup
        result = deduplicate_facts(new, existing, threshold=0.3)
        assert len(result) == 0

    def test_default_threshold_constant(self):
        assert DEFAULT_SIMILARITY_THRESHOLD == 0.85


# ── Merge-level dedup ──────────────────────────────────────────────────────

class TestMergeLevelDedup:
    def test_merge_dedup_near_duplicates_across_batches(self):
        """When merging turn-level facts with per-tool facts, near-duplicates collapse."""
        per_tool_facts = [
            {"content": "src/main.py is a FastAPI application entry point module"},
        ]
        turn_level_facts = [
            # This is a near-duplicate (>0.85 Jaccard with per-tool fact)
            {"content": "src/main.py is a FastAPI application entry point", "fact_type": "observation"},
            # This is distinct
            {"content": "Auth module uses JWT tokens for authentication", "fact_type": "observation"},
        ]

        # Deduplicate turn-level against per-tool
        result = deduplicate_facts(turn_level_facts, per_tool_facts)

        # Should drop the first (near-dup) and keep the second
        assert len(result) == 1
        assert result[0]["content"] == "Auth module uses JWT tokens for authentication"
