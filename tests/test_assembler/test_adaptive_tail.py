"""Tests for Adaptive Tail Sizing by Intent."""

import pytest

from archolith_proxy.assembler.tail import (
    classify_turn_intent,
    smart_tail,
)


# =============================================================================
# classify_turn_intent Tests
# =============================================================================

class TestClassifyTurnIntent:
    def test_continue_signals(self):
        """Should detect continuation intent."""
        assert classify_turn_intent("continue what we were doing") == "continue"
        assert classify_turn_intent("fix the failing test") == "continue"
        assert classify_turn_intent("now do the same for the other file") == "continue"
        assert classify_turn_intent("keep going with the plan") == "continue"
        assert classify_turn_intent("also add the validation") == "continue"

    def test_pivot_signals(self):
        """Should detect pivot / reset intent."""
        assert classify_turn_intent("start fresh") == "pivot"
        assert classify_turn_intent("let's start a new feature") == "pivot"
        assert classify_turn_intent("ignore everything above") == "pivot"
        assert classify_turn_intent("start over") == "pivot"
        assert classify_turn_intent("forget that, let's do something different") == "pivot"

    def test_neutral_signals(self):
        """Should return neutral for unclear messages."""
        assert classify_turn_intent("hello") == "neutral"
        assert classify_turn_intent("what is this?") == "neutral"
        assert classify_turn_intent("") == "neutral"
        assert classify_turn_intent("can you explain this code?") == "neutral"

    def test_case_insensitive(self):
        """Should be case insensitive."""
        assert classify_turn_intent("CONTINUE what we were doing") == "continue"
        assert classify_turn_intent("Start Fresh") == "pivot"


# =============================================================================
# smart_tail with Intent Adjustment Tests
# =============================================================================

class TestSmartTailWithIntent:
    def test_continue_expands_tail(self):
        """Continue intent should expand the tail."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
            {"role": "assistant", "content": "Response 3"},
            {"role": "user", "content": "Turn 4"},
            {"role": "assistant", "content": "Response 4"},
            {"role": "user", "content": "Turn 5"},
        ]

        # base_size=3, adjustment=2 → effective size should be 5
        result = smart_tail(
            messages,
            base_size=3,
            intent="continue",
            intent_adjustment=2,
        )
        assert len(result) >= 5

    def test_pivot_shrinks_tail(self):
        """Pivot intent should shrink the tail."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
            {"role": "assistant", "content": "Response 3"},
            {"role": "user", "content": "Turn 4"},
            {"role": "assistant", "content": "Response 4"},
            {"role": "user", "content": "Turn 5"},
        ]

        # base_size=5, adjustment=3, min_size=2 → effective size should be 2
        result = smart_tail(
            messages,
            base_size=5,
            intent="pivot",
            intent_adjustment=3,
            min_size=2,
        )
        assert len(result) <= 5  # Should be smaller than original

    def test_neutral_keeps_original_size(self):
        """Neutral intent should not change the tail size."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
        ]

        result = smart_tail(
            messages,
            base_size=3,
            intent="neutral",
            intent_adjustment=4,
        )
        assert len(result) == 3

    def test_min_size_respected_on_pivot(self):
        """Pivot should never go below min_size."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
            {"role": "assistant", "content": "Response 3"},
        ]

        result = smart_tail(
            messages,
            base_size=5,
            intent="pivot",
            intent_adjustment=10,  # Very aggressive shrink
            min_size=3,
        )
        assert len(result) >= 3

    def test_no_intent_uses_original_behavior(self):
        """When intent is None, should behave exactly like before."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
        ]

        result_with_none = smart_tail(messages, base_size=3, intent=None)
        result_without = smart_tail(messages, base_size=3)

        assert len(result_with_none) == len(result_without)


# =============================================================================
# Integration / Edge Case Tests
# =============================================================================

class TestAdaptiveTailEdgeCases:
    def test_empty_messages(self):
        result = smart_tail([], base_size=5, intent="continue")
        assert result == []

    def test_adjustment_zero(self):
        """Adjustment of 0 should have no effect."""
        messages = [{"role": "user", "content": f"Turn {i}"} for i in range(10)]

        result = smart_tail(
            messages,
            base_size=5,
            intent="continue",
            intent_adjustment=0,
        )
        assert len(result) == 5

    def test_very_large_adjustment(self):
        """Should handle very large adjustments gracefully."""
        messages = [{"role": "user", "content": f"Turn {i}"} for i in range(20)]

        result = smart_tail(
            messages,
            base_size=5,
            intent="continue",
            intent_adjustment=100,
        )
        # Should not exceed message length
        assert len(result) <= len(messages)