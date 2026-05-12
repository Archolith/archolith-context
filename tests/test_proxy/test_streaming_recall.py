"""Tests for streaming recall tool interception (Phase 5b).

Covers:
1. StreamingToolCallAccumulator — reassembles streaming tool_call deltas
2. stream_with_recall_detection — detects recall tool calls in SSE stream
3. _assemble_streaming_response — converts buffered chunks to response dict
4. _non_streaming_to_sse — converts response dict to SSE format
5. Full streaming recall interception flow (via ASGI transport)
"""

import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from src.main import create_app, _metrics
from src.proxy.streaming import (
    ResponseCapture,
    StreamingToolCallAccumulator,
    StreamingRecallResult,
    stream_with_recall_detection,
    _parse_sse_line,
    _assemble_streaming_response,
    _non_streaming_to_sse,
)
from src.proxy.tool_injection import RECALL_TOOL_NAME


# --- StreamingToolCallAccumulator tests ---

class TestStreamingToolCallAccumulator:
    """Test reassembly of streaming tool_call deltas into complete objects."""

    def test_single_tool_call(self):
        acc = StreamingToolCallAccumulator()
        # First chunk: id + name
        acc.add_delta([{
            "index": 0,
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "__context_engine_recall", "arguments": ""},
        }])
        # Argument chunks
        acc.add_delta([{
            "index": 0,
            "function": {"arguments": '{"ques'},
        }])
        acc.add_delta([{
            "index": 0,
            "function": {"arguments": 'tion": "test"}'},
        }])
        acc.mark_complete()

        assert acc.is_complete
        assert len(acc.tool_calls) == 1
        tc = acc.tool_calls[0]
        assert tc["id"] == "call_abc123"
        assert tc["function"]["name"] == "__context_engine_recall"
        assert json.loads(tc["function"]["arguments"]) == {"question": "test"}

    def test_multiple_tool_calls(self):
        acc = StreamingToolCallAccumulator()
        acc.add_delta([{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": ""},
        }])
        acc.add_delta([{
            "index": 1,
            "id": "call_2",
            "type": "function",
            "function": {"name": "__context_engine_recall", "arguments": ""},
        }])
        acc.add_delta([{
            "index": 1,
            "function": {"arguments": '{"question": "api"}'},
        }])
        acc.mark_complete()

        assert acc.first_tool_name == "read_file"  # Index 0 comes first
        assert len(acc.tool_calls) == 2
        assert acc.tool_calls[1]["function"]["name"] == "__context_engine_recall"

    def test_first_tool_name_none_initially(self):
        acc = StreamingToolCallAccumulator()
        assert acc.first_tool_name is None

    def test_first_tool_name_after_name_chunk(self):
        acc = StreamingToolCallAccumulator()
        acc.add_delta([{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "__context_engine_recall", "arguments": ""},
        }])
        assert acc.first_tool_name == "__context_engine_recall"

    def test_empty_delta(self):
        acc = StreamingToolCallAccumulator()
        acc.add_delta([])
        assert acc.first_tool_name is None
        assert len(acc.tool_calls) == 0

    def test_non_dict_delta_ignored(self):
        acc = StreamingToolCallAccumulator()
        acc.add_delta(["not a dict", 123, None])
        assert len(acc.tool_calls) == 0


# --- _parse_sse_line tests ---

class TestParseSSELine:
    def test_data_line(self):
        data = {"choices": [{"delta": {"content": "hello"}}]}
        line = f"data: {json.dumps(data)}"
        result = _parse_sse_line(line)
        assert result is not None
        assert result["choices"][0]["delta"]["content"] == "hello"

    def test_done_line(self):
        result = _parse_sse_line("data: [DONE]")
        assert result is None

    def test_non_data_line(self):
        result = _parse_sse_line("event: ping")
        assert result is None

    def test_invalid_json(self):
        result = _parse_sse_line("data: {invalid json}")
        assert result is None

    def test_empty_line(self):
        result = _parse_sse_line("")
        assert result is None


# --- _assemble_streaming_response tests ---

class TestAssembleStreamingResponse:
    def test_content_only(self):
        chunks = [
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"content": " world"}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {}, "finish_reason": "stop"}]}),
        ]
        acc = StreamingToolCallAccumulator()
        result = _assemble_streaming_response(chunks, acc)
        assert result["model"] == "test"
        assert result["id"] == "chatcmpl-1"
        assert result["choices"][0]["message"]["content"] == "Hello world"
        assert result["choices"][0]["message"]["role"] == "assistant"
        assert result["choices"][0]["finish_reason"] == "stop"

    def test_with_tool_calls(self):
        chunks = [
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "__context_engine_recall", "arguments": ""}}]}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"question":"api"}'}}]}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        acc = StreamingToolCallAccumulator()
        acc.add_delta([{"index": 0, "id": "call_1", "type": "function", "function": {"name": "__context_engine_recall", "arguments": ""}}])
        acc.add_delta([{"index": 0, "function": {"arguments": '{"question":"api"}'}}])
        acc.mark_complete()

        result = _assemble_streaming_response(chunks, acc)
        msg = result["choices"][0]["message"]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "__context_engine_recall"
        assert result["choices"][0]["finish_reason"] == "tool_calls"


# --- _non_streaming_to_sse tests ---

class TestNonStreamingToSSE:
    def test_content_response(self):
        response = {
            "id": "chatcmpl-1",
            "created": 1000,
            "model": "test",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop",
            }],
        }
        lines = _non_streaming_to_sse(response)
        assert len(lines) >= 3  # role + content + finish + [DONE]
        assert any("Hello world" in line for line in lines)
        assert lines[-1] == "data: [DONE]"

    def test_tool_call_response(self):
        response = {
            "id": "chatcmpl-1",
            "created": 1000,
            "model": "test",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"/foo"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        lines = _non_streaming_to_sse(response)
        assert len(lines) >= 3
        # Should have name delta and arguments delta
        all_text = "\n".join(lines)
        assert "read_file" in all_text
        assert "tool_calls" in all_text
        assert lines[-1] == "data: [DONE]"


# --- stream_with_recall_detection tests ---

class TestStreamWithRecallDetection:
    """Test the buffer-and-decide streaming recall detection."""

    @pytest.fixture
    def mock_streaming_response(self):
        """Create a mock httpx.Response that yields SSE lines."""
        async def _make_response(lines: list[str]):
            response = MagicMock(spec=httpx.Response)
            response.status_code = 200

            async def aiter_lines():
                for line in lines:
                    yield line

            response.aiter_lines = aiter_lines
            return response

        return _make_response

    @pytest.mark.asyncio
    async def test_content_stream_passthrough(self, mock_streaming_response):
        """When the model produces content (not a tool call), stream should relay lines."""
        sse_lines = [
            'data: {"id":"c1","model":"test","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        resp = await mock_streaming_response(sse_lines)

        yielded_lines = []
        recall_result = None
        capture = None

        async for line, result, cap in stream_with_recall_detection(resp, "__context_engine_recall"):
            if result is not None:
                recall_result = result
            if cap is not None:
                capture = cap
            if line:
                yielded_lines.append(line)

        # Should have relayed all lines
        assert len(yielded_lines) == 5
        assert recall_result is None  # No recall detected
        assert capture is not None
        assert capture.get_full_text() == "Hello world"

    @pytest.mark.asyncio
    async def test_recall_tool_call_detected(self, mock_streaming_response):
        """When the model calls __context_engine_recall, should be detected."""
        sse_lines = [
            'data: {"id":"c1","model":"test","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"__context_engine_recall","arguments":""}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"question\\":"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"api key\\"}"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        resp = await mock_streaming_response(sse_lines)

        recall_result = None
        async for line, result, cap in stream_with_recall_detection(resp, "__context_engine_recall"):
            if result is not None and result.is_recall:
                recall_result = result
                break

        assert recall_result is not None
        assert recall_result.is_recall
        assert recall_result.accumulator.first_tool_name == "__context_engine_recall"
        tc = recall_result.accumulator.tool_calls[0]
        assert tc["id"] == "call_1"
        assert json.loads(tc["function"]["arguments"]) == {"question": "api key"}

    @pytest.mark.asyncio
    async def test_other_tool_call_passthrough(self, mock_streaming_response):
        """When the model calls a different tool, stream should relay lines."""
        sse_lines = [
            'data: {"id":"c1","model":"test","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_file","arguments":""}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\"/foo\\"}"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        resp = await mock_streaming_response(sse_lines)

        yielded_lines = []
        recall_result = None
        async for line, result, cap in stream_with_recall_detection(resp, "__context_engine_recall"):
            if result is not None:
                recall_result = result
            if line:
                yielded_lines.append(line)

        assert recall_result is None
        assert len(yielded_lines) == 5  # All lines relayed

    @pytest.mark.asyncio
    async def test_empty_stream(self, mock_streaming_response):
        """Empty stream should yield no lines and an empty capture."""
        resp = await mock_streaming_response([])

        yielded_lines = []
        capture = None
        async for line, result, cap in stream_with_recall_detection(resp, "__context_engine_recall"):
            if cap is not None:
                capture = cap
            if line:
                yielded_lines.append(line)

        assert len(yielded_lines) == 0
        assert capture is not None
        assert capture.get_full_text() == ""


# --- Full streaming recall interception via ASGI transport ---

class TestStreamingRecallInterception:
    """Integration tests for streaming recall interception through the full proxy."""

    def _make_sse_lines(self, response_data: dict) -> list[str]:
        """Helper: convert a response dict to SSE-format lines for mock upstream."""
        lines = []
        # Role chunk
        lines.append(f"data: {json.dumps({'id': response_data['id'], 'object': 'chat.completion.chunk', 'created': response_data.get('created', 0), 'model': response_data.get('model', ''), 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}")
        # Content or tool_calls chunks
        msg = response_data["choices"][0]["message"]
        if msg.get("content"):
            lines.append(f"data: {json.dumps({'id': response_data['id'], 'object': 'chat.completion.chunk', 'created': response_data.get('created', 0), 'model': response_data.get('model', ''), 'choices': [{'index': 0, 'delta': {'content': msg['content']}, 'finish_reason': None}]})}")
        if msg.get("tool_calls"):
            for i, tc in enumerate(msg["tool_calls"]):
                lines.append(f"data: {json.dumps({'id': response_data['id'], 'object': 'chat.completion.chunk', 'created': response_data.get('created', 0), 'model': response_data.get('model', ''), 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': i, 'id': tc['id'], 'type': tc.get('type', 'function'), 'function': {'name': tc['function']['name'], 'arguments': ''}}]}, 'finish_reason': None}]})}")
                if tc['function'].get('arguments'):
                    lines.append(f"data: {json.dumps({'id': response_data['id'], 'object': 'chat.completion.chunk', 'created': response_data.get('created', 0), 'model': response_data.get('model', ''), 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': i, 'function': {'arguments': tc['function']['arguments']}}]}, 'finish_reason': None}]})}")
        # Final chunk
        lines.append(f"data: {json.dumps({'id': response_data['id'], 'object': 'chat.completion.chunk', 'created': response_data.get('created', 0), 'model': response_data.get('model', ''), 'choices': [{'index': 0, 'delta': {}, 'finish_reason': response_data['choices'][0].get('finish_reason', 'stop')}]})}")
        lines.append("data: [DONE]")
        return lines

    @pytest.mark.asyncio
    async def test_streaming_with_no_recall_tool(self):
        """When recall tool is not injected, streaming should work normally."""
        app = create_app()

        # Mock upstream that returns streaming content
        sse_lines = self._make_sse_lines({
            "id": "chatcmpl-1",
            "model": "test",
            "created": 1000,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Normal response"},
                "finish_reason": "stop",
            }],
        })

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/chat/completions" in url_str:
                body = json.loads(request.content)
                if body.get("stream"):
                    # Return SSE stream
                    async def content():
                        for line in sse_lines:
                            yield (line + "\n\n").encode()
                    return httpx.Response(200, content=content(), headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json={
                    "id": "chatcmpl-1",
                    "model": "test",
                    "created": 1000,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Normal response"}, "finish_reason": "stop"}],
                })
            return httpx.Response(404)

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.extractor_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False  # Skip session resolution

            # Make a streaming request WITHOUT recall tool
            # Use client.stream() to ensure body is consumed within context
            asgi_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            async with asgi_client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                # Collect the stream
                chunks = []
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and not line.endswith("[DONE]"):
                        chunks.append(line)
            await asgi_client.aclose()

        # Should have content chunks
        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_streaming_recall_detection_passthrough(self):
        """When recall tool is injected but model produces content, stream relays normally."""
        app = create_app()

        # Mock upstream that returns content (not a recall tool call)
        sse_lines = self._make_sse_lines({
            "id": "chatcmpl-1",
            "model": "test",
            "created": 1000,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "I'll help with that."},
                "finish_reason": "stop",
            }],
        })

        second_response = {
            "id": "chatcmpl-2",
            "model": "test",
            "created": 1000,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "I'll help with that."},
                "finish_reason": "stop",
            }],
        }

        request_count = [0]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "/chat/completions" in url_str:
                request_count[0] += 1
                body = json.loads(request.content)
                if body.get("stream"):
                    async def content():
                        for line in sse_lines:
                            yield (line + "\n\n").encode()
                    return httpx.Response(200, content=content(), headers={"content-type": "text/event-stream"})
                # Non-streaming fallback
                return httpx.Response(200, json=second_response)
            return httpx.Response(404)

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.extractor_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False

            asgi_client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            # With recall tool injected (but model won't call it)
            async with asgi_client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                    "tools": [{"type": "function", "function": {"name": "__context_engine_recall", "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}}],
                },
            ) as resp:
                assert resp.status_code == 200
                full_text = ""
                async for line in resp.aiter_lines():
                    if line.startswith("data: ") and not line.endswith("[DONE]"):
                        try:
                            data = json.loads(line[6:])
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            if delta.get("content"):
                                full_text += delta["content"]
                        except (json.JSONDecodeError, IndexError):
                            pass
            await asgi_client.aclose()

        # Model produced content, should be relayed
        assert "I'll help with that" in full_text
