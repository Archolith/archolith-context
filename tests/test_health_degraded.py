"""Tests for D4 — honest /health on degraded graph startup.

A graph backend that was configured but failed to initialize must be reported as
``degraded`` (503), not silently as ``ok``/``not_configured``. With
require_graph_on_startup, such a failure aborts startup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from archolith_proxy.config import Settings, reset_settings


class TestHealthDegraded:
    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_health_degraded_when_graph_init_failed(self, app, client):
        """graph_degraded_reason set + graph not ready -> 503 degraded."""
        app.state.graph_degraded_reason = "ladybug init failed: boom"
        with patch("archolith_proxy.main.is_graph_ready", return_value=False):
            resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["graph"] == "degraded"
        assert "boom" in data["graph_degraded_reason"]

    @pytest.mark.asyncio
    async def test_health_ok_when_not_configured(self, client):
        """No graph configured (no degraded reason) -> 200 ok / not_configured."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["graph"] == "not_configured"

    @pytest.mark.asyncio
    async def test_health_ok_when_graph_connected(self, client):
        mock_backend = AsyncMock()
        mock_backend.is_ready.return_value = True
        mock_backend.verify_connectivity.return_value = True
        with patch("archolith_proxy.main.is_graph_ready", return_value=True), \
             patch("archolith_proxy.main.get_backend", return_value=mock_backend):
            resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["graph"] == "connected"


class TestRequireGraphOnStartup:
    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_startup_aborts_when_required_graph_fails(self, app):
        """require_graph_on_startup + ladybug init failure -> lifespan raises."""
        settings = Settings(
            graph_backend="ladybug",
            require_graph_on_startup=True,
            filter_enabled=False,
        )
        with patch("archolith_proxy.main.get_settings", return_value=settings), \
             patch("archolith_proxy.main.init_backend", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(RuntimeError):
                async with app.router.lifespan_context(app):
                    pass

    @pytest.mark.asyncio
    async def test_startup_survives_when_not_required(self, app):
        """Default (require_graph_on_startup False) -> degraded, not aborted."""
        settings = Settings(
            graph_backend="ladybug",
            require_graph_on_startup=False,
            filter_enabled=False,
        )
        with patch("archolith_proxy.main.get_settings", return_value=settings), \
             patch("archolith_proxy.main.init_backend", new=AsyncMock(side_effect=RuntimeError("boom"))):
            async with app.router.lifespan_context(app):
                assert app.state.graph_degraded_reason is not None
                assert "boom" in app.state.graph_degraded_reason
