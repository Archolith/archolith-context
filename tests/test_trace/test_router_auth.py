"""Tests for trace router authentication."""

import pytest


@pytest.mark.asyncio
async def test_metrics_401_without_token(monkeypatch, client):
    """GET /metrics returns 401 when ADMIN_TOKEN is set and no token provided."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret-token")
    # Note: need to create a new app with the env var set, so we refresh settings
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/metrics")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_metrics_200_with_bearer_token(monkeypatch, client):
    """GET /metrics returns 200 when valid Bearer token provided."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret-token")
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/metrics",
            headers={"Authorization": "Bearer test-secret-token"}
        )
        assert response.status_code == 200
        assert "proxy" in response.json()


@pytest.mark.asyncio
async def test_metrics_200_with_x_admin_token(monkeypatch, client):
    """GET /metrics returns 200 when valid X-Admin-Token header provided."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret-token")
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/metrics",
            headers={"X-Admin-Token": "test-secret-token"}
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_trace_sessions_401_without_token(monkeypatch, client):
    """GET /trace/sessions returns 401 when ADMIN_TOKEN is set and no token provided."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret-token")
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/trace/sessions")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_trace_sessions_200_with_token(monkeypatch, client):
    """GET /trace/sessions returns 200 when valid token provided."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret-token")
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get(
            "/trace/sessions",
            headers={"Authorization": "Bearer test-secret-token"}
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_qa_extract_401_without_token(monkeypatch, client):
    """POST /trace/qa/extract returns 401 when ADMIN_TOKEN is set and no token provided."""
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret-token")
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/trace/qa/extract",
            json={"user_message": "test"}
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_qa_extract_429_when_in_flight(monkeypatch, client):
    """POST /trace/qa/extract returns 429 when extraction already in flight."""
    # Use default (no token required) for this test
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    from archolith_proxy.config import reset_settings
    reset_settings()
    from archolith_proxy.main import create_app
    from httpx import ASGITransport
    import httpx

    app = create_app()
    transport = ASGITransport(app=app)

    # Set the in-flight flag manually to simulate an ongoing extraction
    from archolith_proxy.trace import router as trace_router_module
    trace_router_module._extraction_in_flight = True

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/trace/qa/extract",
                json={"user_message": "test", "assistant_response": "response"}
            )
            assert response.status_code == 429
            data = response.json()
            assert "already in flight" in data.get("error", "").lower()
    finally:
        # Clear the flag after test
        trace_router_module._extraction_in_flight = False
