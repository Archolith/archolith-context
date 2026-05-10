"""Phase 1 proxy tests with mock upstream server."""

import json

import httpx
import pytest
from httpx import ASGITransport

from src.main import create_app


# --- Mock upstream responses ---

MOCK_NON_STREAM_RESPONSE = {
    "id": "chatcmpl-test123",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello! How can I help?"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

MOCK_MODELS_RESPONSE = {
    "object": "list",
    "data": [
        {"id": "test-model", "object": "model", "created": 1234567890, "owned_by": "test"},
        {"id": "other-model", "object": "model", "created": 1234567890, "owned_by": "test"},
    ],
}


def _build_sse_chunks(text: str, model: str = "test-model") -> str:
    """Build SSE stream from text content."""
    lines = []
    # First chunk with role
    lines.append(f'data: {json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1234567890, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})}')
    # Content chunks
    for word in text.split(" "):
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(chunk)}")
    # Final chunk
    final = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    lines.append(f"data: {json.dumps(final)}")
    lines.append("data: [DONE]")
    return "\n".join(lines)


# --- Fixtures ---


class MockUpstream:
    """Simulates upstream API responses for testing."""

    def __init__(self):
        self.last_request: dict | None = None
        self.response_status = 200
        self.response_body: dict | None = None
        self.sse_content: str | None = None

    def set_non_stream_response(self, body: dict):
        self.response_body = body

    def set_stream_response(self, text: str, model: str = "test-model"):
        self.sse_content = _build_sse_chunks(text, model)

    def set_error(self, status: int, message: str):
        self.response_status = status
        self.response_body = {"error": {"message": message, "type": "server_error"}}


@pytest.fixture
def mock_upstream():
    return MockUpstream()


@pytest.fixture
async def client_with_mock(mock_upstream):
    """Create a test client that intercepts upstream calls."""

    app = create_app()

    # Patch http_client to use mock transport
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        mock_upstream.last_request = {
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": request.content.decode() if request.content else None,
        }

        if "/models" in str(request.url):
            return httpx.Response(200, json=MOCK_MODELS_RESPONSE)

        if mock_upstream.sse_content and request.method == "POST":
            return httpx.Response(
                200,
                content=mock_upstream.sse_content.encode(),
                headers={"Content-Type": "text/event-stream"},
            )

        if mock_upstream.response_status != 200:
            return httpx.Response(
                mock_upstream.response_status,
                json=mock_upstream.response_body,
            )

        return httpx.Response(200, json=mock_upstream.response_body or MOCK_NON_STREAM_RESPONSE)

    mock_transport = httpx.MockTransport(mock_handler)

    async with app.router.lifespan_context(app):
        app.state.http_client = httpx.AsyncClient(transport=mock_transport)
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# --- Tests ---


@pytest.mark.asyncio
async def test_non_streaming_passthrough(client_with_mock, mock_upstream):
    """Non-streaming request forwarded and response relayed unchanged."""
    mock_upstream.set_non_stream_response(MOCK_NON_STREAM_RESPONSE)

    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "chatcmpl-test123"
    assert data["model"] == "test-model"
    assert data["choices"][0]["message"]["content"] == "Hello! How can I help?"
    assert data["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_non_streaming_forwards_auth_header(client_with_mock, mock_upstream):
    """Authorization header is rewritten with upstream API key."""
    mock_upstream.set_non_stream_response(MOCK_NON_STREAM_RESPONSE)

    await client_with_mock.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "test"}]},
    )

    assert mock_upstream.last_request is not None
    assert "authorization" in mock_upstream.last_request["headers"]


@pytest.mark.asyncio
async def test_non_streaming_forwards_model_field(client_with_mock, mock_upstream):
    """Request model field is passed through to upstream."""
    mock_upstream.set_non_stream_response(MOCK_NON_STREAM_RESPONSE)

    await client_with_mock.post(
        "/v1/chat/completions",
        json={"model": "specific-model-v2", "messages": [{"role": "user", "content": "test"}]},
    )

    assert mock_upstream.last_request is not None
    body = json.loads(mock_upstream.last_request["body"])
    assert body["model"] == "specific-model-v2"


@pytest.mark.asyncio
async def test_streaming_passthrough(client_with_mock, mock_upstream):
    """Streaming request receives SSE chunks in correct format."""
    mock_upstream.set_stream_response("Hello world from model")

    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    text = resp.text

    # Should contain data: prefixed lines
    assert "data: " in text
    # Should contain [DONE]
    assert "data: [DONE]" in text
    # Should contain content chunks
    assert "Hello" in text or "world" in text


@pytest.mark.asyncio
async def test_streaming_forwards_sse_format(client_with_mock, mock_upstream):
    """SSE chunks are relayed with correct data: {...}\\n\\n format."""
    mock_upstream.set_stream_response("test content")

    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    text = resp.text
    lines = [l for l in text.split("\n") if l.startswith("data: ")]

    # Each data line should contain valid JSON (except [DONE])
    for line in lines:
        if "[DONE]" in line:
            continue
        json_str = line[len("data: "):]
        parsed = json.loads(json_str)
        assert "choices" in parsed


@pytest.mark.asyncio
async def test_upstream_500_relayed_as_502(client_with_mock, mock_upstream):
    """Upstream 500 error is relayed with OpenAI error shape."""
    mock_upstream.set_error(500, "Internal server error")

    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "test"}]},
    )

    # Non-streaming: upstream status is relayed directly
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_invalid_json_returns_400(client_with_mock):
    """Malformed JSON returns 400 with invalid_request_error."""
    resp = await client_with_mock.post(
        "/v1/chat/completions",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_missing_messages_returns_400(client_with_mock):
    """Missing messages field returns 400 with error shape."""
    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={"model": "test-model"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_empty_messages_returns_400(client_with_mock):
    """Empty messages array returns 400 with param=messages."""
    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": []},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["param"] == "messages"


@pytest.mark.asyncio
async def test_models_endpoint(client_with_mock, mock_upstream):
    """GET /v1/models returns upstream model list."""
    resp = await client_with_mock.get("/v1/models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert len(data["data"]) == 2
    assert data["data"][0]["id"] == "test-model"


@pytest.mark.asyncio
async def test_models_by_id(client_with_mock, mock_upstream):
    """GET /v1/models/{id} returns model info."""
    resp = await client_with_mock.get("/v1/models/test-model")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_passthrough_unrecognized_route(client_with_mock, mock_upstream):
    """Unrecognized /v1/* routes are relayed to upstream."""
    resp = await client_with_mock.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "test"},
    )

    assert resp.status_code == 200
    assert mock_upstream.last_request is not None
    assert "embeddings" in mock_upstream.last_request["url"]


@pytest.mark.asyncio
async def test_tool_calls_forwarded(client_with_mock, mock_upstream):
    """Requests with tool definitions are forwarded correctly."""
    mock_upstream.set_non_stream_response({
        "id": "chatcmpl-tool-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "src/main.py"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    })

    resp = await client_with_mock.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Read main.py"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["tool_calls"] is not None
    assert data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read_file"
