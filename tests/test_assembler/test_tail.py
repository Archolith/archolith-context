"""Unit tests for Smart Tail — structural integrity for coherence tails."""

import pytest

from archolith_proxy.assembler.tail import smart_tail, _find_assistant_with_tool_call


class TestFindAssistantWithToolCall:
    def test_finds_matching_assistant(self):
        """Should find the assistant message that issued a tool_call_id."""
        messages = [
            {"role": "user", "content": "Run this"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "output", "tool_call_id": "call_abc"},
            {"role": "assistant", "content": "Done"},
        ]
        result = _find_assistant_with_tool_call(messages, "call_abc", search_before=3)
        assert result == 1

    def test_returns_none_when_not_found(self):
        """Should return None if no assistant has the matching tool_call_id."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "tool", "content": "result", "tool_call_id": "call_missing"},
        ]
        result = _find_assistant_with_tool_call(messages, "call_missing", search_before=3)
        assert result is None

    def test_does_not_search_at_or_after_search_before(self):
        """Should only search indices before search_before."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "output", "tool_call_id": "call_abc"},
        ]
        # search_before=1 means we only look at index 0
        result = _find_assistant_with_tool_call(messages, "call_abc", search_before=1)
        assert result == 0

    def test_search_before_0_returns_none(self):
        """No messages to search before index 0."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]},
        ]
        result = _find_assistant_with_tool_call(messages, "call_abc", search_before=0)
        assert result is None


class TestSmartTail:
    def test_no_tool_messages_stays_at_base_size(self):
        """Tail with no tool messages should remain at base_size."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 3"},
        ]
        result = smart_tail(messages, base_size=3)
        assert len(result) == 3
        assert result[-1]["content"] == "Turn 3"

    def test_orphaned_tool_message_expands_tail(self):
        """Tail starting with orphaned tool message should expand to include matching assistant."""
        messages = [
            {"role": "user", "content": "Run this"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "result: 42", "tool_call_id": "call_abc"},
            {"role": "assistant", "content": "The answer is 42"},
            {"role": "user", "content": "Next question"},
        ]
        # base_size=2 would give [assistant("The answer"), user("Next")]
        # But the tool message at index 2 is between the matching assistant (1) and the tail
        # Actually base_size=2 gives last 2: [assistant, user] — no tool message in tail
        # Let's use base_size=3 to get [tool, assistant, user] which has the orphan
        result = smart_tail(messages, base_size=3)
        # Should expand to include the assistant at index 1
        assert len(result) >= 4  # expanded beyond base_size
        # The assistant with tool_calls should be in the result
        assistant_msgs = [m for m in result if m.get("role") == "assistant" and m.get("tool_calls")]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["tool_calls"][0]["id"] == "call_abc"

    def test_multi_tool_call_sequence_expands(self):
        """Tail with assistant → tool → tool → assistant → user should all be included."""
        messages = [
            {"role": "user", "content": "Run these"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "result 1", "tool_call_id": "call_1"},
            {"role": "tool", "content": "result 2", "tool_call_id": "call_2"},
            {"role": "assistant", "content": "Both done"},
            {"role": "user", "content": "Great"},
        ]
        # base_size=3 gives [tool_2, assistant, user] — orphaned tool_2
        result = smart_tail(messages, base_size=3)
        # Should expand to include assistant with tool_calls and tool_1
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assistant_with_calls = [m for m in result if m.get("role") == "assistant" and m.get("tool_calls")]
        # All tool messages should have their matching assistant
        if tool_msgs:
            assert len(assistant_with_calls) >= 1

    def test_expansion_hits_max_falls_back(self):
        """When expansion exceeds max_size, fall back to fixed tail."""
        # Create a message array where expansion would need many messages
        messages = [
            {"role": "user", "content": f"Turn {i}"} if i % 2 == 0
            else {"role": "assistant", "content": f"Response {i}"}
            for i in range(30)
        ]
        # Insert a tool message early that would require huge expansion
        messages.insert(1, {
            "role": "assistant", "content": "", "tool_calls": [
                {"id": "call_early", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ],
        })
        messages.insert(2, {"role": "tool", "content": "old result", "tool_call_id": "call_early"})

        # base_size=3, max_size=5 — expansion would need ~30 messages
        result = smart_tail(messages, base_size=3, max_size=5)
        # Should fall back to fixed tail of size 3
        assert len(result) == 3

    def test_empty_messages(self):
        """Empty message list should return empty list."""
        result = smart_tail([], base_size=3)
        assert result == []

    def test_messages_shorter_than_base_size(self):
        """When messages < base_size, all messages are kept."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        result = smart_tail(messages, base_size=5)
        assert len(result) == 2

    def test_tool_message_without_tool_call_id_ignored(self):
        """Tool messages without tool_call_id should not trigger expansion."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "tool", "content": "orphan result"},  # No tool_call_id
            {"role": "assistant", "content": "Response 2"},
            {"role": "user", "content": "Turn 2"},
        ]
        result = smart_tail(messages, base_size=3)
        # Should not expand — tool message has no tool_call_id
        assert len(result) == 3

    def test_no_matching_assistant_strips_orphan(self):
        """When no assistant matches a tool_call_id, the orphan should be present
        but not cause infinite expansion (defensive behavior)."""
        messages = [
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "No tool calls here"},
            {"role": "tool", "content": "orphan", "tool_call_id": "call_nonexistent"},
            {"role": "assistant", "content": "Response"},
            {"role": "user", "content": "Turn 2"},
        ]
        # base_size=3 gives [tool, assistant, user]
        # The tool has a tool_call_id but no matching assistant — _find returns None
        # No expansion needed, tail stays at 3
        result = smart_tail(messages, base_size=3)
        assert len(result) == 3
