"""CORS middleware behavior for operator-safe defaults."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport


async def _preflight(app, origin: str):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.options(
            "/live",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )


@pytest.mark.asyncio
async def test_default_cors_allows_loopback_origin(app):
    response = await _preflight(app, "http://127.0.0.1:5173")

    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


@pytest.mark.asyncio
async def test_default_cors_rejects_arbitrary_origin(app):
    response = await _preflight(app, "https://evil.example")

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_explicit_cors_origin_allows_only_that_origin(monkeypatch):
    from archolith_proxy.config import reset_settings
    from archolith_proxy.main import create_app

    reset_settings()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://dashboard.example"]')
    app = create_app()

    allowed = await _preflight(app, "https://dashboard.example")
    rejected = await _preflight(app, "http://127.0.0.1:5173")

    assert allowed.headers["access-control-allow-origin"] == "https://dashboard.example"
    assert "access-control-allow-origin" not in rejected.headers
