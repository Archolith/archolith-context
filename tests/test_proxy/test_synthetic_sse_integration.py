"""Integration tests for the forced-non-streaming → SSE conversion path.

When synthetic_tools_enabled=True and the client sends stream=True, the proxy:
  1. Injects synthetic tool defs into the request
  2. Forces stream=False to upstream
  3. Lets handle_non_streaming_synthetic intercept any synthetic tool calls
  4. Converts the final JSON response back to SSE via _wrap_response_as_sse

These tests verify that non-synthetic (real) tool calls survive that conversion
and reach the streaming client as proper OpenAI-spec SSE tool_call deltas.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.config import get_settings
from archolith_proxy.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_sse_chunks(text: str) -> list[dict]:
    """Parse SSE stream body into a list of parsed JSON chunk dicts."""
    chunks = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            chunks.append(json.loads(line[6:]))
    return chunks


def _tool_call_deltas(chunks: list[dict]) -> list[dict]:
    """Return all delta dicts that carry tool_calls."""
    result = []
    for chunk in chunks:
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("tool_calls"):
                result.extend(delta["tool_calls"])
    return result


def _finish_reasons(chunks: list[dict]) -> list[str | None]:
    return [
        choice.get("finish_reason")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if choice.get("finish_reason") is not None
    ]


# ---------------------------------------------------------------------------
# Shared mock backend factory
# ---------------------------------------------------------------------------

def _make_mock_backend():
    backend = AsyncMock()
    backend.get_turn_number = AsyncMock(return_value=1)
    backend.find_session_by_id = AsyncMock(return_value=None)
    backend.create_session = AsyncMock()
    backend.touch_session = AsyncMock()
    return backend


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSyntheticSseIntegration:
    """End-to-end tests for the forced-non-streaming → SSE path."""

    @pytest.mark.asyncio
    async def test_non_synthetic_tool_call_survives_sse_conversion(self):
        """streaming client + synthetic tools enabled + upstream returns a real tool call.

        The proxy must force non-streaming, leave the Bash tool call untouched,
        convert to SSE, and deliver proper name + args + finish deltas to the client.
        """
        SESSION_ID = "test-session-ns-001"

        upstream_response = {
            "id": "chatcmpl-real001",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_bash001",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": "{\"command\": \"ls -la\"}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            # Proxy forces stream=False — return non-streaming JSON
            return httpx.Response(200, json=upstream_response)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        settings.synthetic_tools_enabled = True

        try:
            mock_backend = _make_mock_backend()
            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": [{"role": "user", "content": "list files"}],
                                "stream": True,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")

            body = resp.text
            assert "data: [DONE]" in body

            chunks = _parse_sse_chunks(body)
            assert chunks, "Expected at least one SSE data chunk"

            tc_deltas = _tool_call_deltas(chunks)
            assert tc_deltas, "Expected tool_call deltas in SSE stream"

            # Name delta: id, type, function.name
            name_deltas = [d for d in tc_deltas if d.get("function", {}).get("name")]
            assert name_deltas, "Expected at least one tool_call name delta"
            assert name_deltas[0]["function"]["name"] == "Bash"
            assert name_deltas[0].get("id") == "call_bash001"

            # Arguments delta
            args_deltas = [
                d for d in tc_deltas
                if d.get("function", {}).get("arguments") not in (None, "")
            ]
            assert args_deltas, "Expected at least one tool_call arguments delta"
            assert "command" in args_deltas[0]["function"]["arguments"]

            # Finish reason
            assert "tool_calls" in _finish_reasons(chunks)

        finally:
            settings.synthetic_tools_enabled = original_synthetic

    @pytest.mark.asyncio
    async def test_mixed_calls_strips_synthetic_preserves_real_tool_call(self):
        """streaming client + synthetic tools enabled + upstream returns mixed tool calls.

        The model calls both recall_session_work (synthetic) and Read (real) in
        the same response. The proxy strips the synthetic tool, converts to SSE,
        and delivers only the Read tool_call deltas to the client.
        """
        SESSION_ID = "test-session-mix-002"

        upstream_response = {
            "id": "chatcmpl-mix001",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_synthetic001",
                            "type": "function",
                            "function": {"name": "recall_session_work", "arguments": "{}"},
                        },
                        {
                            "id": "call_real001",
                            "type": "function",
                            "function": {"name": "Read", "arguments": "{\"file_path\": \"/tmp/foo.py\"}"},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=upstream_response)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        settings.synthetic_tools_enabled = True

        try:
            mock_backend = _make_mock_backend()
            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": [{"role": "user", "content": "read a file"}],
                                "stream": True,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")

            body = resp.text
            assert "data: [DONE]" in body

            chunks = _parse_sse_chunks(body)
            tc_deltas = _tool_call_deltas(chunks)
            assert tc_deltas, "Expected tool_call deltas in SSE stream"

            tool_names = [
                d.get("function", {}).get("name")
                for d in tc_deltas
                if d.get("function", {}).get("name")
            ]

            # Synthetic tool must be stripped
            assert "recall_session_work" not in tool_names, (
                "recall_session_work should be stripped from the SSE stream"
            )
            # Real tool must survive
            assert "Read" in tool_names, (
                "Read tool call should be present in the SSE stream"
            )

        finally:
            settings.synthetic_tools_enabled = original_synthetic

    @pytest.mark.asyncio
    async def test_streaming_passthrough_unaffected_when_synthetic_disabled(self):
        """With synthetic_tools_enabled=False (default), streaming is normal SSE passthrough.

        Upstream receives stream=True and the client gets standard SSE content chunks
        — the forced-non-streaming path is never triggered.
        """
        sse_content = "\n".join([
            'data: {"id":"chatcmpl-t","object":"chat.completion.chunk","created":1234567890,"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-t","object":"chat.completion.chunk","created":1234567890,"model":"test-model","choices":[{"index":0,"delta":{"content":"hello world"},"finish_reason":null}]}',
            'data: {"id":"chatcmpl-t","object":"chat.completion.chunk","created":1234567890,"model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])

        upstream_stream_used = []

        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            upstream_stream_used.append(body.get("stream", False))
            return httpx.Response(
                200,
                content=sse_content.encode(),
                headers={"Content-Type": "text/event-stream"},
            )

        # synthetic_tools_enabled defaults to False — no override needed
        settings = get_settings()
        assert not settings.synthetic_tools_enabled, "This test requires synthetic_tools_enabled=False"

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(mock_handler)
            )
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )

        assert resp.status_code == 200

        # Upstream must have received stream=True — no forced non-streaming
        assert upstream_stream_used, "Expected upstream to have been called"
        assert upstream_stream_used[-1] is True, (
            f"Expected upstream stream=True but got {upstream_stream_used[-1]}"
        )

        body = resp.text
        assert "hello world" in body
        assert "data: [DONE]" in body
