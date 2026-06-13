"""Tests for smart_tail's over-max turn-boundary fallback (Phase 2).

When expanding the tail to preserve tool-call integrity would exceed max_size,
smart_tail falls back to a turn boundary instead of a naive fixed slice: it
takes the last max_size messages, advances to the first user message in that
window, or — if there is none — past any leading orphaned tool messages.

Each construction below is built to deterministically force
expanded_size > max_size so the fallback branch is actually exercised.
"""

from archolith_proxy.assembler.tail import smart_tail


class TestSmartTailTurnBoundaryFallback:
    def test_fallback_advances_to_first_user_in_window(self):
        """Over-max fallback starts at the first user message inside the window."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "tool", "content": "result", "tool_call_id": "call_1"},
        ]
        # base tail (last 3) holds the orphaned tool -> expands to index 0 ->
        # expanded_size 7 > max_size 5 -> fallback.
        result = smart_tail(messages, base_size=3, max_size=5)
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "u2"

    def test_fallback_skips_leading_orphaned_tool_when_no_user(self):
        """With no user in the window, leading orphaned tool messages are skipped."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "user", "content": "u1"},
            {"role": "tool", "content": "r1", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
        # base tail holds the orphaned tool -> expands to index 0 ->
        # expanded_size 5 > max_size 3 -> fallback; window = last 3 has no user.
        result = smart_tail(messages, base_size=3, max_size=3)
        assert len(result) > 0
        assert result[0]["role"] != "tool"
        assert result[0]["content"] == "a1"

    def test_fallback_skips_tool_without_tool_call_id(self):
        """A tool message lacking tool_call_id is always orphaned and skipped."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "user", "content": "u1"},
            {"role": "tool", "content": "r1", "tool_call_id": "call_1"},
            {"role": "tool", "content": "orphan, no id"},
            {"role": "assistant", "content": "a1"},
        ]
        result = smart_tail(messages, base_size=3, max_size=3)
        assert result[0]["role"] != "tool"
        assert all(m.get("role") != "tool" for m in result)

    def test_fallback_all_orphaned_tools_yields_empty(self):
        """If every message in the window is an orphaned tool, the tail is empty."""
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_far", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            ]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_near", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "rn", "tool_call_id": "call_near"},
            {"role": "tool", "content": "rf", "tool_call_id": "call_far"},
        ]
        # Expands to index 0 (size 4) > max_size 2; window = last 2 are both
        # tools whose matching assistants sit before the window -> all orphaned.
        result = smart_tail(messages, base_size=2, max_size=2)
        assert result == []

    def test_no_fallback_returns_normal_path_unchanged(self):
        """When expansion stays within max_size, the normal path is unchanged."""
        messages = [
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        result = smart_tail(messages, base_size=3, max_size=20)
        # No tool messages -> no expansion, no fallback: last 3 messages verbatim.
        assert result == messages[2:]
        assert len(result) == 3
