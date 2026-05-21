"""Unit tests for session recall tool injection and interception."""

import json

import pytest

from archolith_proxy.proxy.tool_injection import (
    RECALL_TOOL_DEFINITION,
    RECALL_TOOL_NAME,
    build_tool_result_message,
    find_recall_tool_call,
    inject_recall_tool,
    strip_recall_from_response,
    strip_recall_tool,
)


class TestInjectRecallTool:
    def test_injects_into_empty_tools(self):
        body = {}
        result = inject_recall_tool(body)
        assert RECALL_TOOL_DEFINITION in result["tools"]
        assert len(result["tools"]) == 1

    def test_injects_into_existing_tools(self):
        existing_tool = {
            "type": "function",
            "function": {"name": "read_file", "parameters": {}},
        }
        body = {"tools": [existing_tool]}
        result = inject_recall_tool(body)
        assert len(result["tools"]) == 2
        assert result["tools"][0] == existing_tool
        assert result["tools"][1] == RECALL_TOOL_DEFINITION

    def test_idempotent_double_inject(self):
        body = {}
        inject_recall_tool(body)
        inject_recall_tool(body)
        assert len(body["tools"]) == 1

    def test_does_not_override_tool_choice_auto(self):
        body = {"tool_choice": "auto"}
        result = inject_recall_tool(body)
        assert result.get("tool_choice") == "auto"

    def test_does_not_override_tool_choice_required(self):
        body = {"tool_choice": "required"}
        result = inject_recall_tool(body)
        assert result.get("tool_choice") == "required"

    def test_does_not_override_specific_function_tool_choice(self):
        body = {
            "tool_choice": {
                "type": "function",
                "function": {"name": "read_file"},
            }
        }
        result = inject_recall_tool(body)
        assert result["tool_choice"]["function"]["name"] == "read_file"

    def test_does_not_modify_other_body_fields(self):
        body = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
        result = inject_recall_tool(body)
        assert result["model"] == "gpt-4"
        assert len(result["messages"]) == 1


class TestStripRecallTool:
    def test_strips_recall_tool_from_body(self):
        body = {
            "tools": [
                RECALL_TOOL_DEFINITION,
                {"type": "function", "function": {"name": "read_file"}},
            ]
        }
        result = strip_recall_tool(body)
        assert len(result["tools"]) == 1
        assert result["tools"][0]["function"]["name"] == "read_file"

    def test_removes_empty_tools_array(self):
        body = {"tools": [RECALL_TOOL_DEFINITION]}
        result = strip_recall_tool(body)
        assert "tools" not in result

    def test_no_tools_key_is_noop(self):
        body = {"model": "gpt-4"}
        result = strip_recall_tool(body)
        assert "tools" not in result

    def test_no_recall_tool_is_noop(self):
        existing = {"type": "function", "function": {"name": "read_file"}}
        body = {"tools": [existing]}
        result = strip_recall_tool(body)
        assert len(result["tools"]) == 1


class TestFindRecallToolCall:
    def test_finds_recall_tool_call(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": RECALL_TOOL_NAME,
                                    "arguments": '{"question": "What did we decide?"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
        result = find_recall_tool_call(response)
        assert result is not None
        assert result["id"] == "call_123"

    def test_returns_none_when_no_tool_calls(self):
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Hello!"}}
            ]
        }
        assert find_recall_tool_call(response) is None

    def test_returns_none_when_only_other_tool_calls(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_456",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
        assert find_recall_tool_call(response) is None

    def test_returns_none_empty_choices(self):
        assert find_recall_tool_call({}) is None
        assert find_recall_tool_call({"choices": []}) is None

    def test_finds_recall_among_multiple_calls(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_a",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            },
                            {
                                "id": "call_b",
                                "type": "function",
                                "function": {
                                    "name": RECALL_TOOL_NAME,
                                    "arguments": '{"question": "test"}',
                                },
                            },
                            {
                                "id": "call_c",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": "{}"},
                            },
                        ],
                    }
                }
            ]
        }
        result = find_recall_tool_call(response)
        assert result is not None
        assert result["id"] == "call_b"

    def test_skips_non_dict_tool_calls(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": ["not_a_dict"],
                    }
                }
            ]
        }
        assert find_recall_tool_call(response) is None


class TestStripRecallFromResponse:
    def test_strips_recall_tool_call_only(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_recall",
                                "type": "function",
                                "function": {"name": RECALL_TOOL_NAME, "arguments": "{}"},
                            },
                            {
                                "id": "call_other",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            },
                        ],
                    }
                }
            ]
        }
        result = strip_recall_from_response(response)
        remaining = result["choices"][0]["message"]["tool_calls"]
        assert len(remaining) == 1
        assert remaining[0]["function"]["name"] == "read_file"

    def test_removes_tool_calls_field_when_all_are_recall(self):
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_recall",
                                "type": "function",
                                "function": {"name": RECALL_TOOL_NAME, "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
        result = strip_recall_from_response(response)
        assert "tool_calls" not in result["choices"][0]["message"]

    def test_no_tool_calls_is_noop(self):
        response = {
            "choices": [
                {"message": {"role": "assistant", "content": "Hello!"}}
            ]
        }
        result = strip_recall_from_response(response)
        assert "tool_calls" not in result["choices"][0]["message"]

    def test_empty_choices_is_noop(self):
        result = strip_recall_from_response({})
        assert result == {}

    def test_preserves_other_response_fields(self):
        response = {
            "id": "resp_123",
            "model": "gpt-4",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Here is the answer",
                        "tool_calls": [
                            {
                                "id": "call_recall",
                                "type": "function",
                                "function": {"name": RECALL_TOOL_NAME, "arguments": "{}"},
                            }
                        ],
                    }
                }
            ],
        }
        result = strip_recall_from_response(response)
        assert result["id"] == "resp_123"
        assert result["model"] == "gpt-4"
        msg = result["choices"][0]["message"]
        assert msg["content"] == "Here is the answer"
        assert "tool_calls" not in msg


class TestBuildToolResultMessage:
    def test_builds_valid_tool_message(self):
        msg = build_tool_result_message("call_abc", "Found 3 relevant facts.")
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_abc"
        assert msg["content"] == "Found 3 relevant facts."
        assert msg["name"] == RECALL_TOOL_NAME

    def test_empty_result_text(self):
        msg = build_tool_result_message("call_xyz", "")
        assert msg["content"] == ""
        assert msg["tool_call_id"] == "call_xyz"


class TestRecallToolConstants:
    def test_tool_name_prefix(self):
        assert RECALL_TOOL_NAME.startswith("__context_engine_")

    def test_tool_definition_has_required_fields(self):
        assert RECALL_TOOL_DEFINITION["type"] == "function"
        func = RECALL_TOOL_DEFINITION["function"]
        assert func["name"] == RECALL_TOOL_NAME
        assert "description" in func
        params = func["parameters"]
        assert params["type"] == "object"
        assert "question" in params["properties"]
        assert "question" in params["required"]
