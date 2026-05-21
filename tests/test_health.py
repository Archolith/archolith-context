"""Phase 0 health endpoint tests."""

import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "graph" in data


@pytest.mark.asyncio
async def test_health_graph_not_configured(client):
    resp = await client.get("/health")
    data = resp.json()
    assert data["graph"] == "not_configured"


@pytest.mark.asyncio
async def test_invalid_json_returns_400(client):
    resp = await client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data
    assert data["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_missing_messages_returns_400(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "test"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_empty_messages_returns_400(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "invalid_request_error"
    assert data["error"]["param"] == "messages"
