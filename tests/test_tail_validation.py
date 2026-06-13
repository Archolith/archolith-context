"""Unit + integration tests for tool-call-aware coherence-tail validation.

Covers the Phase 1 fix: _validate_tail must preserve a complete leading
assistant(tool_calls) + tool group (the expansion smart_tail performed),
drop only truly orphaned leading tool messages, and never drop an assistant's
tool_calls via same-role merge. Plus the Phase 3 cleanup: rewrite_messages
relies on _ensure_user_first alone for the final user-first guarantee.
"""

from archolith_proxy.proxy.rewrite import _validate_tail, rewrite_messages
from archolith_proxy.models.dtos import AssembledContext


def _assembled():
    return AssembledContext(
        system_message={"role": "system", "content": "graph ctx"},
        graph_context=[{"role": "system", "content": "graph ctx"}],
        coherence_tail=[],
        token_estimate=500,
        facts_retrieved=5,
        session_id="test",
    )


class TestValidateTailToolGroups:
    def test_keeps_leading_tool_call_group(self):
        """A leading assistant(tool_calls)+tool group whose results follow is kept intact."""
        tail = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "file body", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "Done"},
            {"role": "user", "content": "Next"},
        ]
        result = _validate_tail(list(tail))
        assert len(result) == 4
        assert result[0]["role"] == "assistant"
        assert result[0]["tool_calls"][0]["id"] == "call_1"
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "call_1"

    def test_drops_orphaned_leading_tool_keeps_rest(self):
        """A leading orphaned tool message is dropped without dropping the rest of the tail."""
        tail = [
            {"role": "tool", "content": "stale", "tool_call_id": "call_gone"},
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer"},
        ]
        result = _validate_tail(list(tail))
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert all(m["role"] != "tool" for m in result)

    def test_same_role_merge_preserves_tool_calls(self):
        """Consecutive assistants must not merge when the second carries tool_calls."""
        tail = [
            {"role": "user", "content": "Go"},
            {"role": "assistant", "content": "Thinking..."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "ok", "tool_call_id": "call_2"},
            {"role": "user", "content": "More"},
        ]
        result = _validate_tail(list(tail))
        # The two assistant messages must remain distinct; tool_calls preserved.
        assistant_with_calls = [
            m for m in result if m["role"] == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_with_calls) == 1
        assert assistant_with_calls[0]["tool_calls"][0]["id"] == "call_2"
        # The tool result still has its matching assistant present.
        assert any(m.get("tool_call_id") == "call_2" for m in result)

    def test_drops_plain_leading_assistant(self):
        """A plain leading assistant (no tool_calls) is dropped conservatively."""
        tail = [
            {"role": "assistant", "content": "dangling reply"},
            {"role": "user", "content": "Hi"},
        ]
        result = _validate_tail(list(tail))
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_drops_dangling_assistant_tool_calls(self):
        """assistant(tool_calls) with no following tool results is dropped (would 400)."""
        tail = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_9", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            ]},
            {"role": "user", "content": "Hi"},
        ]
        result = _validate_tail(list(tail))
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_plain_consecutive_users_still_merge(self):
        """Regression: plain consecutive same-role messages still merge."""
        tail = [
            {"role": "user", "content": "A"},
            {"role": "user", "content": "B"},
            {"role": "assistant", "content": "C"},
        ]
        result = _validate_tail(list(tail))
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert "A" in result[0]["content"] and "B" in result[0]["content"]


class TestRewriteMessagesPreservesGroup:
    def test_expanded_tool_group_survives_rewrite(self):
        """An expanded leading tool-call group at the tail head survives rewrite_messages
        with its assistant and tool result still matched (no orphaned tool result)."""
        original = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "turn1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "turn2"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "file body", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "turn3"},
        ]
        result = rewrite_messages(original, _assembled(), coherence_tail_size=3)

        # First non-system message must be a user message.
        non_system = [m for m in result if m["role"] != "system"]
        assert non_system[0]["role"] == "user"

        # The assistant tool_call and its tool result both survive and match.
        call_ids = {
            tc["id"]
            for m in result if m.get("tool_calls")
            for tc in m["tool_calls"]
        }
        result_ids = {m["tool_call_id"] for m in result if m.get("tool_call_id")}
        assert "call_1" in call_ids
        assert result_ids <= call_ids, f"orphaned tool results: {result_ids - call_ids}"
