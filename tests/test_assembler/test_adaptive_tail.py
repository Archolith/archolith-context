"""Comprehensive tests for Adaptive Tail Sizing by Intent.

Covers:
- Intent classification (continue, pivot, neutral)
- Tail size adjustment behavior
- Edge cases
- Integration with smart_tail
- Configuration sensitivity
"""

import pytest

from archolith_proxy.assembler.tail import classify_turn_intent, smart_tail


# =============================================================================
# classify_turn_intent Tests
# =============================================================================

class TestClassifyTurnIntent:
    def test_strong_continue_signals(self):
        """Detect clear continuation intent."""
        assert classify_turn_intent("continue what we were doing") == "continue"
        assert classify_turn_intent("fix the failing test") == "continue"
        assert classify_turn_intent("now do the same for the other file") == "continue"
        assert classify_turn_intent("keep going with the current approach") == "continue"
        assert classify_turn_intent("also add error handling") == "continue"

    def test_strong_pivot_signals(self):
        """Detect clear pivot / reset intent."""
        assert classify_turn_intent("start fresh") == "pivot"
        assert classify_turn_intent("let's start a new feature") == "pivot"
        assert classify_turn_intent("ignore everything above") == "pivot"
        assert classify_turn_intent("start over") == "pivot"
        assert classify_turn_intent("forget that, let's do something completely different") == "pivot"

    def test_neutral_messages(self):
        """Return neutral for unclear or generic messages."""
        assert classify_turn_intent("hello") == "neutral"
        assert classify_turn_intent("can you explain this?") == "neutral"
        assert classify_turn_intent("what does this function do?") == "neutral"
        assert classify_turn_intent("") == "neutral"

    def test_case_insensitivity(self):
        assert classify_turn_intent("CONTINUE working on the login") == "continue"
        assert classify_turn_intent("Start Fresh") == "pivot"

    def test_mixed_signals_prefers_stronger(self):
        """If both signals present, prefer the stronger/more explicit one."""
        # "start fresh" is stronger than "continue"
        assert classify_turn_intent("start fresh but continue the old way") == "pivot"


# =============================================================================
# smart_tail Intent Adjustment Tests
# =============================================================================

class TestSmartTailIntentAdjustment:
    def test_continue_increases_tail_size(self):
        """Continue intent should result in a larger tail."""
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(15)
        ]

        # base_size=5, adjustment=4 → expect ~9 messages
        result = smart_tail(
            messages,
            base_size=5,
            intent="continue",
            intent_adjustment=4,
        )
        assert len(result) > 5

    def test_pivot_decreases_tail_size(self):
        """Pivot intent should result in a smaller tail."""
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(15)
        ]

        # base_size=10, adjustment=6, min_size=3 → expect smaller tail
        result = smart_tail(
            messages,
            base_size=10,
            intent="pivot",
            intent_adjustment=6,
            min_size=3,
        )
        assert len(result) < 10

    def test_neutral_keeps_original_size(self):
        """Neutral intent should behave like no adjustment."""
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(10)
        ]

        result_neutral = smart_tail(messages, base_size=5, intent="neutral", intent_adjustment=4)
        result_none = smart_tail(messages, base_size=5)

        assert len(result_neutral) == len(result_none)

    def test_min_size_is_respected(self):
        """Pivot should never produce a tail smaller than min_size."""
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(20)
        ]

        result = smart_tail(
            messages,
            base_size=15,
            intent="pivot",
            intent_adjustment=20,  # Very aggressive
            min_size=4,
        )
        assert len(result) >= 4

    def test_zero_adjustment_has_no_effect(self):
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(10)
        ]

        result = smart_tail(
            messages,
            base_size=5,
            intent="continue",
            intent_adjustment=0,
        )
        assert len(result) == 5

    def test_large_adjustment_is_capped(self):
        """Very large adjustments should not exceed message count."""
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(8)
        ]

        result = smart_tail(
            messages,
            base_size=3,
            intent="continue",
            intent_adjustment=100,
        )
        assert len(result) <= len(messages)


# =============================================================================
# Edge Cases
# =============================================================================

class TestAdaptiveTailEdgeCases:
    def test_empty_message_list(self):
        result = smart_tail([], base_size=5, intent="continue")
        assert result == []

    def test_single_message(self):
        messages = [{"role": "user", "content": "Only message"}]
        result = smart_tail(messages, base_size=5, intent="pivot")
        assert len(result) == 1

    def test_intent_none_preserves_original_behavior(self):
        """Passing intent=None should be identical to not passing it."""
        messages = [
            {"role": "user", "content": f"Turn {i}"} for i in range(10)
        ]

        r1 = smart_tail(messages, base_size=5, intent=None)
        r2 = smart_tail(messages, base_size=5)

        assert len(r1) == len(r2)

    def test_adjustment_with_tool_messages(self):
        """Intent adjustment should still preserve tool-call integrity."""
        messages = [
            {"role": "user", "content": "Run command"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
            {"role": "user", "content": "Continue with the plan"},
        ]

        result = smart_tail(
            messages,
            base_size=2,
            intent="continue",
            intent_adjustment=3,
        )
        # Should still include the assistant + tool pair
        assert any(m.get("role") == "assistant" for m in result)


# =============================================================================
# Integration Style Test
# =============================================================================

def test_full_flow_with_drift_like_messages():
    """Simulate a session that starts continuing then pivots."""
    messages = [
        {"role": "user", "content": "Continue building the auth system"},
        {"role": "assistant", "content": "Working on auth..."},
        {"role": "user", "content": "Start fresh with a completely new dashboard"},
    ]

    # First message → continue
    assert classify_turn_intent(messages[0]["content"]) == "continue"

    # Last message → pivot
    assert classify_turn_intent(messages[-1]["content"]) == "pivot"