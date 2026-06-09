"""Tests for D8 — streaming re-send capture preserves tool_calls.

ResponseCapture.set_non_streaming_response previously stored text only, so a
tool-call-only re-sent response lost its tool_calls (and, with empty content,
extraction was skipped). The capture now preserves tool_calls and the streaming
finalize path falls back to a tool_call summary for response_text.
"""

from __future__ import annotations

from archolith_proxy.openai.streaming import _summarize_tool_calls
from archolith_proxy.proxy.streaming import ResponseCapture


_TOOL_CALL_RESPONSE = {
    "model": "test-model",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": '{"file_path": "a.py"}'},
                    }
                ],
            },
        }
    ],
}


class TestCaptureToolCalls:
    def test_set_non_streaming_response_preserves_tool_calls(self):
        cap = ResponseCapture()
        cap.set_non_streaming_response(_TOOL_CALL_RESPONSE)
        assert len(cap.tool_calls) == 1
        assert cap.tool_calls[0]["function"]["name"] == "Read"
        assert cap.finish_reason == "tool_calls"

    def test_empty_content_with_tool_calls(self):
        cap = ResponseCapture()
        cap.set_non_streaming_response(_TOOL_CALL_RESPONSE)
        # No textual content on a tool-call-only turn.
        assert cap.get_full_text() == ""
        # ...but tool_calls are available for the finalize fallback.
        assert cap.tool_calls

    def test_no_tool_calls_when_text_response(self):
        cap = ResponseCapture()
        cap.set_non_streaming_response({
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "done"}}],
        })
        assert cap.tool_calls == []
        assert cap.get_full_text() == "done"


class TestSummarizeToolCalls:
    def test_summary_includes_name_and_args(self):
        summary = _summarize_tool_calls(_TOOL_CALL_RESPONSE["choices"][0]["message"]["tool_calls"])
        assert "[tool_call] Read(" in summary
        assert "a.py" in summary

    def test_dict_arguments_serialized(self):
        summary = _summarize_tool_calls([
            {"function": {"name": "Bash", "arguments": {"command": "ls"}}}
        ])
        assert "[tool_call] Bash(" in summary
        assert "ls" in summary

    def test_empty_list(self):
        assert _summarize_tool_calls([]) == ""
