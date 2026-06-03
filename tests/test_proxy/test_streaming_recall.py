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

from archolith_proxy.main import create_app
from archolith_proxy.metrics import get_metrics
from archolith_proxy.proxy.streaming import (
    ResponseCapture,
    StreamingToolCallAccumulator,
    StreamingRecallResult,
    stream_with_recall_detection,
    _parse_sse_line,
    _assemble_streaming_response,
    _non_streaming_to_sse,
)
from archolith_proxy.proxy.tool_injection import RECALL_TOOL_NAME


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
            "function": {"name": "__archolith_recall", "arguments": ""},
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
        assert tc["function"]["name"] == "__archolith_recall"
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
            "function": {"name": "__archolith_recall", "arguments": ""},
        }])
        acc.add_delta([{
            "index": 1,
            "function": {"arguments": '{"question": "api"}'},
        }])
        acc.mark_complete()

        assert acc.first_tool_name == "read_file"  # Index 0 comes first
        assert len(acc.tool_calls) == 2
        assert acc.tool_calls[1]["function"]["name"] == "__archolith_recall"

    def test_first_tool_name_none_initially(self):
        acc = StreamingToolCallAccumulator()
        assert acc.first_tool_name is None

    def test_first_tool_name_after_name_chunk(self):
        acc = StreamingToolCallAccumulator()
        acc.add_delta([{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "__archolith_recall", "arguments": ""},
        }])
        assert acc.first_tool_name == "__archolith_recall"

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
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "__archolith_recall", "arguments": ""}}]}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"question":"api"}'}}]}, "finish_reason": None}]}),
            json.dumps({"model": "test", "id": "chatcmpl-1", "created": 1000, "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        ]
        acc = StreamingToolCallAccumulator()
        acc.add_delta([{"index": 0, "id": "call_1", "type": "function", "function": {"name": "__archolith_recall", "arguments": ""}}])
        acc.add_delta([{"index": 0, "function": {"arguments": '{"question":"api"}'}}])
        acc.mark_complete()

        result = _assemble_streaming_response(chunks, acc)
        msg = result["choices"][0]["message"]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "__archolith_recall"
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

        async for line, result, cap in stream_with_recall_detection(resp, "__archolith_recall"):
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
        """When the model calls __archolith_recall, should be detected."""
        sse_lines = [
            'data: {"id":"c1","model":"test","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"__archolith_recall","arguments":""}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"question\\":"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"api key\\"}"}}]},"finish_reason":null}]}',
            'data: {"id":"c1","model":"test","choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        resp = await mock_streaming_response(sse_lines)

        recall_result = None
        async for line, result, cap in stream_with_recall_detection(resp, "__archolith_recall"):
            if result is not None and result.is_recall:
                recall_result = result
                break

        assert recall_result is not None
        assert recall_result.is_recall
        assert recall_result.accumulator.first_tool_name == "__archolith_recall"
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
        async for line, result, cap in stream_with_recall_detection(resp, "__archolith_recall"):
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
        async for line, result, cap in stream_with_recall_detection(resp, "__archolith_recall"):
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
                    "tools": [{"type": "function", "function": {"name": "__archolith_recall", "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}}],
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

    @pytest.mark.asyncio
    async def test_streaming_recall_interception_full_flow(self):
        """E2E: model calls __archolith_recall in stream → proxy intercepts →
        executes recall → re-sends non-streaming → converts response to SSE →
        client receives final content.

        This tests the actual recall interception path that had two critical bugs
        (P2-1: stream:true retained in re-send body; P2-2: extraction never fires
        because get_full_text() returns empty for non-streaming format).
        """
        import os
        from unittest.mock import AsyncMock, patch, MagicMock

        # Enable session recall tool via env override
        env_patch = patch.dict(os.environ, {"SESSION_RECALL_TOOL_ENABLED": "true"})
        env_patch.start()

        try:
            from archolith_proxy.config import reset_settings
            reset_settings()

            app = create_app()

            # First streaming response: model calls __archolith_recall
            recall_sse_lines = self._make_sse_lines({
                "id": "chatcmpl-1",
                "model": "test",
                "created": 1000,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_recall_1",
                            "type": "function",
                            "function": {
                                "name": "__archolith_recall",
                                "arguments": '{"question":"api key setup"}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            })

            # Second (non-streaming) response: model answers with content
            second_response = {
                "id": "chatcmpl-2",
                "model": "test",
                "created": 1000,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Based on your session history, the API key is stored in .env.",
                    },
                    "finish_reason": "stop",
                }],
            }

            request_count = [0]

            async def mock_handler(request: httpx.Request) -> httpx.Response:
                request_count[0] += 1
                url_str = str(request.url)
                if "/chat/completions" in url_str:
                    body = json.loads(request.content)
                    if body.get("stream"):
                        # First request: streaming with recall tool call
                        async def content():
                            for line in recall_sse_lines:
                                yield (line + "\n\n").encode()
                        return httpx.Response(
                            200,
                            content=content(),
                            headers={"content-type": "text/event-stream"},
                        )
                    else:
                        # Re-send from proxy (non-streaming): return content
                        # Verify P2-1 fix: the re-send must have stream=false
                        assert body.get("stream") is False, (
                            f"P2-1 regression: re-send body still has stream={body.get('stream')}"
                        )
                        # Verify recall tool was stripped from tools
                        tools = body.get("tools", [])
                        tool_names = [t.get("function", {}).get("name") for t in tools]
                        assert "__archolith_recall" not in tool_names, (
                            "Recall tool should be stripped from re-send tools"
                        )
                        # Verify tool result message is appended
                        messages = body.get("messages", [])
                        tool_results = [m for m in messages if m.get("role") == "tool"]
                        assert len(tool_results) >= 1, "Tool result message should be appended"
                        return httpx.Response(200, json=second_response)
                # Other requests (e.g., extraction, embeddings) — return dummy OK
                return httpx.Response(200, json={"id": "dummy", "choices": []})

            mock_transport = httpx.MockTransport(mock_handler)

            # Separate transport for extractor — always returns valid extraction response
            async def extractor_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "id": "ext-1",
                    "model": "gpt-4.1-mini",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"facts":[],"files_touched":[],"decisions":[],"invalidated":[]}'},
                        "finish_reason": "stop",
                    }],
                })

            extractor_transport = httpx.MockTransport(extractor_handler)

            async with app.router.lifespan_context(app):
                app.state.http_client = httpx.AsyncClient(transport=mock_transport)
                app.state.extractor_client = httpx.AsyncClient(transport=extractor_transport)
                with patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                     patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                     patch("archolith_proxy.openai.chat.get_backend") as mock_get_backend, \
                     patch("archolith_proxy.proxy.tool_injection.handle_recall_tool_call", new_callable=AsyncMock) as mock_recall, \
                     patch("archolith_proxy.proxy.locks.wait_for_prior_extraction", new_callable=AsyncMock):

                    mock_backend = AsyncMock()
                    mock_backend.get_turn_number.return_value = 1
                    mock_backend.find_session_by_id.return_value = None
                    mock_backend.get_active_facts.return_value = []
                    mock_backend.get_active_fact_count.return_value = 0
                    mock_backend.find_matching_fact_ids.return_value = []
                    mock_backend.invalidate_facts.return_value = 0
                    mock_get_backend.return_value = mock_backend
                    mock_resolve.return_value = ("test-session-123", True)
                    mock_recall.return_value = "Recalled: API key is in .env file at project root."

                    asgi_client = httpx.AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="http://test",
                    )

                    # Send streaming request with recall tool in tools array
                    # (the proxy should also inject it, but having it already
                    # makes the test more explicit about what triggers interception)
                    async with asgi_client.stream(
                        "POST",
                        "/v1/chat/completions",
                        json={
                            "model": "test",
                            "messages": [
                                {"role": "user", "content": "Where is my API key?"},
                            ],
                            "stream": True,
                        },
                    ) as resp:
                        assert resp.status_code == 200

                        # Collect SSE lines from the final response
                        full_text = ""
                        sse_data_lines = []
                        async for line in resp.aiter_lines():
                            if line.startswith("data: ") and not line.endswith("[DONE]"):
                                sse_data_lines.append(line)
                                try:
                                    data = json.loads(line[6:])
                                    choices = data.get("choices", [])
                                    if choices:
                                        # Streaming format: delta.content
                                        delta = choices[0].get("delta", {})
                                        content = delta.get("content")
                                        if content:
                                            full_text += content
                                        # Non-streaming converted: message.content
                                        message = choices[0].get("message", {})
                                        if message.get("content"):
                                            full_text += message["content"]
                                except (json.JSONDecodeError, IndexError):
                                    pass

                    await asgi_client.aclose()

            # --- Assertions ---

            # The recall tool was actually intercepted
            mock_recall.assert_awaited_once()
            call_args = mock_recall.call_args
            assert call_args.kwargs.get("question") == "api key setup" or \
                   (len(call_args.args) >= 3 and call_args.args[2] == "api key setup"), \
                   f"Expected question 'api key setup', got {call_args}"

            # Two upstream requests: first streaming, second non-streaming re-send
            assert request_count[0] == 2, f"Expected 2 upstream requests, got {request_count[0]}"

            # Client received the final response content (converted to SSE)
            assert "API key" in full_text, f"Expected final content about API key, got: {full_text}"

            # SSE lines were produced (the non-streaming response was converted to SSE)
            assert len(sse_data_lines) > 0, "Should have SSE data lines in final response"

        finally:
            env_patch.stop()
            reset_settings()
