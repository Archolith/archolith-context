"""Proxy RTK integration tests."""

import json
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.config import reset_settings
from archolith_proxy.main import create_app


MOCK_RESPONSE = {
    "id": "chatcmpl-rtk-test",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


@pytest.fixture
async def client_with_mock(monkeypatch):
    reset_settings()
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-test")

    app = create_app()
    captured: dict[str, str] = {}

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json=MOCK_RESPONSE)

    async with app.router.lifespan_context(app):
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, captured


@pytest.mark.asyncio
async def test_rtk_enabled_filters_outbound_tool_messages(client_with_mock, monkeypatch):
    client, captured = client_with_mock
    monkeypatch.setenv("RTK_ENABLED", "true")
    reset_settings()

    def fake_filter_output(text: str, *, tool: str = "", **_: object) -> SimpleNamespace:
        return SimpleNamespace(output=f"filtered:{tool}:{text[:12]}")

    monkeypatch.setattr("archolith_proxy.rtk._filter_output_fn", False)
    monkeypatch.setattr("archolith_proxy.rtk._load_filter_output", lambda: fake_filter_output)

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "summarize the tool output"},
                {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": "x" * 2000},
            ],
        },
    )

    assert resp.status_code == 200
    upstream_body = json.loads(captured["body"])
    assert upstream_body["messages"][1]["content"].startswith("filtered:read_file:")


@pytest.mark.asyncio
async def test_rtk_disabled_leaves_tool_messages_unchanged(client_with_mock, monkeypatch):
    client, captured = client_with_mock
    monkeypatch.setenv("RTK_ENABLED", "false")
    reset_settings()

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "summarize the tool output"},
                {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": "x" * 2000},
            ],
        },
    )

    assert resp.status_code == 200
    upstream_body = json.loads(captured["body"])
    assert upstream_body["messages"][1]["content"] == "x" * 2000
