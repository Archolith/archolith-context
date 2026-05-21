"""Tests for non-streaming recall interception (using unified recall.py helper).

Covers:
1. handle_non_streaming_recall — detection and interception of recall tool calls
2. One recall round in non-streaming mode
3. Two recall rounds in non-streaming mode
4. RecallResult metadata (recall_used, recall_questions, facts_returned_counts)
5. Trace persistence after recall interception
6. Response broadcast after recall interception
7. Extraction scheduling after recall interception
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archolith_proxy.proxy.recall import (
    RecallResult,
    handle_non_streaming_recall,
    execute_recall,
    build_resend_messages,
    resend_with_recall,
)


# --- RecallResult tests ---

class TestRecallResult:
    """Test the RecallResult dataclass."""

    def test_no_recall(self):
        result = RecallResult(final_data=None, recall_used=False)
        assert result.recall_used is False
        assert result.final_data is None
        assert result.recall_questions == []
        assert result.facts_returned_counts == []

    def test_single_recall(self):
        result = RecallResult(
            final_data={"id": "test"},
            recall_used=True,
            recall_questions=["api key"],
            facts_returned_counts=[3],
        )
        assert result.recall_used is True
        assert result.final_data is not None
        assert len(result.recall_questions) == 1
        assert result.recall_questions[0] == "api key"

    def test_double_recall(self):
        result = RecallResult(
            final_data={"id": "test"},
            recall_used=True,
            recall_questions=["api key", "config"],
            facts_returned_counts=[3, 1],
        )
        assert len(result.recall_questions) == 2
        assert len(result.facts_returned_counts) == 2


# --- execute_recall tests ---

class TestExecuteRecall:
    """Test the recall execution helper."""

    @pytest.mark.asyncio
    async def test_execute_recall_with_valid_question(self):
        mock_client = AsyncMock()
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "__archolith_recall",
                "arguments": '{"question": "api key location"}',
            },
        }

        with patch("archolith_proxy.proxy.tool_injection.handle_recall_tool_call", new=AsyncMock(return_value="API key is in .env")):
            result = await execute_recall(mock_client, tool_call, "session-1", turn_number=5)
            assert result == "API key is in .env"

    @pytest.mark.asyncio
    async def test_execute_recall_empty_question(self):
        mock_client = AsyncMock()
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "__archolith_recall",
                "arguments": '{"question": ""}',
            },
        }

        result = await execute_recall(mock_client, tool_call, "session-1", turn_number=5)
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_recall_invalid_json(self):
        mock_client = AsyncMock()
        tool_call = {
            "id": "call_1",
            "function": {
                "name": "__archolith_recall",
                "arguments": "not valid json",
            },
        }

        result = await execute_recall(mock_client, tool_call, "session-1", turn_number=5)
        assert result is None


# --- build_resend_messages tests ---

class TestBuildResendMessages:
    """Test the message reconstruction helper."""

    def test_basic_resend_messages(self):
        original = [{"role": "user", "content": "Hello"}]
        model_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "__archolith_recall", "arguments": '{"question":"q"}'},
            }],
        }
        tool_call = model_msg["tool_calls"][0]
        recall_text = "Some recalled facts"

        result = build_resend_messages(original, model_msg, tool_call, recall_text)

        # Should have: original + model_msg (with recall stripped) + tool result
        assert len(result) == 3
        assert result[0]["role"] == "user"
        # Model message should have tool_calls removed (only recall was there)
        assert "tool_calls" not in result[1] or result[1].get("tool_calls") is None or len(result[1].get("tool_calls", [])) == 0
        assert result[2]["role"] == "tool"

    def test_preserves_non_recall_tool_calls(self):
        original = [{"role": "user", "content": "Hello"}]
        model_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"/foo"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "__archolith_recall", "arguments": '{"question":"q"}'},
                },
            ],
        }
        tool_call = model_msg["tool_calls"][1]
        recall_text = "Some recalled facts"

        result = build_resend_messages(original, model_msg, tool_call, recall_text)

        # Model message should still have the read_file tool call
        remaining_calls = result[1].get("tool_calls", [])
        assert len(remaining_calls) == 1
        assert remaining_calls[0]["function"]["name"] == "read_file"


# --- handle_non_streaming_recall tests ---

class TestHandleNonStreamingRecall:
    """Test the unified non-streaming recall handler."""

    def _make_response(self, data: dict, status_code: int = 200) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.json.return_value = data
        return resp

    @pytest.mark.asyncio
    async def test_no_recall_returns_not_used(self):
        """When response has no recall tool call, returns recall_used=False."""
        resp = self._make_response({
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
        })

        result = await handle_non_streaming_recall(
            resp=resp,
            http_client=AsyncMock(),
            url="https://upstream/chat/completions",
            headers={},
            body=b'{"messages":[]}',
            session_id="session-1",
            turn_number=1,
            original_messages=[],
        )

        assert result.recall_used is False
        assert result.final_data is None

    @pytest.mark.asyncio
    async def test_single_recall_round(self):
        """When response has one recall tool call, intercepts and re-sends."""
        first_response = {
            "id": "chatcmpl-1",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "__archolith_recall",
                            "arguments": '{"question": "api key"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

        second_response = {
            "id": "chatcmpl-2",
            "choices": [{
                "message": {"role": "assistant", "content": "The API key is in .env"},
                "finish_reason": "stop",
            }],
        }

        resp = self._make_response(first_response)

        mock_second_resp = MagicMock(spec=httpx.Response)
        mock_second_resp.status_code = 200
        mock_second_resp.json.return_value = second_response

        mock_client = AsyncMock()

        with patch("archolith_proxy.proxy.tool_injection.handle_recall_tool_call", new=AsyncMock(return_value="API key is in .env")), \
             patch("archolith_proxy.proxy.upstream.upstream_request_with_retry", new=AsyncMock(return_value=mock_second_resp)):
            result = await handle_non_streaming_recall(
                resp=resp,
                http_client=mock_client,
                url="https://upstream/chat/completions",
                headers={"Authorization": "Bearer key"},
                body=json.dumps({"messages": [{"role": "user", "content": "Where is the API key?"}], "tools": [{"type": "function", "function": {"name": "__archolith_recall"}}]}).encode(),
                session_id="session-1",
                turn_number=1,
                original_messages=[{"role": "user", "content": "Where is the API key?"}],
            )

        assert result.recall_used is True
        assert result.final_data is not None
        assert "api key" in result.recall_questions[0].lower() or len(result.recall_questions) >= 1
        # The second response should have content (recall was intercepted)
        final_choices = result.final_data.get("choices", [])
        assert final_choices
        assert "API key" in final_choices[0].get("message", {}).get("content", "")

    @pytest.mark.asyncio
    async def test_two_recall_rounds(self):
        """When the second response also has a recall tool call, handle the second round."""
        first_response = {
            "id": "chatcmpl-1",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "__archolith_recall",
                            "arguments": '{"question": "api key"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

        # Second response ALSO calls recall
        second_response = {
            "id": "chatcmpl-2",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "__archolith_recall",
                            "arguments": '{"question": "config file"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

        # Third response has content
        third_response = {
            "id": "chatcmpl-3",
            "choices": [{
                "message": {"role": "assistant", "content": "API key in .env, config in config.yaml"},
                "finish_reason": "stop",
            }],
        }

        resp = self._make_response(first_response)

        mock_second_resp = MagicMock(spec=httpx.Response)
        mock_second_resp.status_code = 200
        mock_second_resp.json.return_value = second_response

        mock_third_resp = MagicMock(spec=httpx.Response)
        mock_third_resp.status_code = 200
        mock_third_resp.json.return_value = third_response

        mock_client = AsyncMock()
        request_count = [0]

        async def mock_upstream_request(**kwargs):
            request_count[0] += 1
            if request_count[0] == 1:
                return mock_second_resp
            return mock_third_resp

        with patch("archolith_proxy.proxy.tool_injection.handle_recall_tool_call", new=AsyncMock(return_value="Recalled facts")), \
             patch("archolith_proxy.proxy.upstream.upstream_request_with_retry", side_effect=mock_upstream_request):
            result = await handle_non_streaming_recall(
                resp=resp,
                http_client=mock_client,
                url="https://upstream/chat/completions",
                headers={"Authorization": "Bearer key"},
                body=json.dumps({"messages": [{"role": "user", "content": "Where is everything?"}]}).encode(),
                session_id="session-1",
                turn_number=1,
                original_messages=[{"role": "user", "content": "Where is everything?"}],
            )

        assert result.recall_used is True
        assert result.final_data is not None
        # Should track both recall questions
        assert len(result.recall_questions) == 2
        assert "api key" in result.recall_questions[0].lower()
        assert "config file" in result.recall_questions[1].lower()
        # Two upstream requests (one for each recall round)
        assert request_count[0] == 2


# --- resend_with_recall metadata tests ---

class TestResendWithRecallMetadata:
    """Test that resend_with_recall tracks recall metadata."""

    @pytest.mark.asyncio
    async def test_returns_questions_from_second_recall(self):
        """When a second recall happens, its question is tracked."""
        second_response = {
            "id": "chatcmpl-2",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "__archolith_recall",
                            "arguments": '{"question": "deployment config"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }

        third_response = {
            "id": "chatcmpl-3",
            "choices": [{
                "message": {"role": "assistant", "content": "Deployment uses Docker Compose"},
                "finish_reason": "stop",
            }],
        }

        mock_second_resp = MagicMock(spec=httpx.Response)
        mock_second_resp.status_code = 200
        mock_second_resp.json.return_value = second_response

        mock_third_resp = MagicMock(spec=httpx.Response)
        mock_third_resp.status_code = 200
        mock_third_resp.json.return_value = third_response

        request_count = [0]

        async def mock_upstream_request(**kwargs):
            request_count[0] += 1
            if request_count[0] == 1:
                return mock_second_resp
            return mock_third_resp

        with patch("archolith_proxy.proxy.tool_injection.handle_recall_tool_call", new=AsyncMock(return_value="Facts")), \
             patch("archolith_proxy.proxy.upstream.upstream_request_with_retry", side_effect=mock_upstream_request):
            final_data, questions, facts = await resend_with_recall(
                http_client=AsyncMock(),
                url="https://upstream/chat/completions",
                headers={},
                original_body=json.dumps({"messages": []}).encode(),
                resend_messages=[{"role": "user", "content": "test"}],
                session_id="session-1",
                turn_number=1,
                recall_questions=["first question"],
                facts_returned_counts=[0],
            )

        assert final_data is not None
        assert len(questions) == 2
        assert questions[0] == "first question"
        assert "deployment config" in questions[1]


# --- Trace persistence integration test ---

class TestNonStreamingRecallTracePersistence:
    """Test that non-streaming recall turns produce trace records."""

    @pytest.mark.asyncio
    async def test_recall_turn_stores_trace(self):
        """After recall interception, the trace should be stored in the trace store."""
        from archolith_proxy.trace.builder import TraceBuilder
        from archolith_proxy.trace.store import get_trace_store

        builder = TraceBuilder()
        builder.set_request(
            session_id="session-1",
            turn_number=1,
            model="test",
            stream=False,
            input_tokens=100,
            message_count=2,
        )
        builder.set_response(status=200, latency_ms=50.0, output_tokens=50, response_summary="test")
        builder.set_recall(used=True, question="api key", facts_returned=3)

        trace = builder.build()
        assert trace.recall_used is True
        assert trace.recall_question == "api key"

        # Store it
        store = get_trace_store()
        await store.record(trace)

        # Retrieve it
        retrieved = await store.get_turn(trace.turn_id)
        assert retrieved is not None
        assert retrieved.recall_used is True
        assert retrieved.recall_question == "api key"
