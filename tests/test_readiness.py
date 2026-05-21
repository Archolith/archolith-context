"""Tests for liveness and readiness probes.

Covers:
- /live always returns 200
- /ready returns graph + upstream status
- /ready returns 503 when graph backend is disconnected
- /ready reports graph as not_configured when no backend is initialized
- /health legacy endpoint returns ok with graph status
"""

from unittest.mock import AsyncMock, patch

import pytest

from archolith_proxy.config import reset_settings


class TestLivenessProbe:
    """Liveness probe should always return 200."""

    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_live_returns_200(self, client):
        resp = await client.get("/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "alive"

    @pytest.mark.asyncio
    async def test_live_includes_version(self, client):
        resp = await client.get("/live")
        data = resp.json()
        assert "version" in data

    @pytest.mark.asyncio
    async def test_live_includes_uptime(self, client):
        resp = await client.get("/live")
        data = resp.json()
        assert "uptime_s" in data


class TestReadinessProbe:
    """Readiness probe should check graph + upstream."""

    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_ready_graph_not_configured(self, client):
        """When no graph backend, graph status should be not_configured."""
        resp = await client.get("/ready")
        data = resp.json()
        assert data["graph"] == "not_configured"

    @pytest.mark.asyncio
    async def test_ready_reports_upstream_status(self, client):
        """Readiness should include upstream field."""
        resp = await client.get("/ready")
        data = resp.json()
        assert "upstream" in data

    @pytest.mark.asyncio
    async def test_ready_503_when_upstream_unreachable(self, client):
        """When upstream is unreachable, /ready should return 503."""
        resp = await client.get("/ready")
        # In test mode, upstream is not configured so it will be unreachable
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert "upstream_unreachable" in data.get("reasons", [])

    @pytest.mark.asyncio
    async def test_ready_graph_connected_with_mock_backend(self, client):
        """When graph backend is ready and verify_connectivity returns True."""
        mock_backend = AsyncMock()
        mock_backend.is_ready.return_value = True
        mock_backend.verify_connectivity.return_value = True

        with patch("archolith_proxy.main.is_graph_ready", return_value=True), \
             patch("archolith_proxy.main.get_backend", return_value=mock_backend):
            resp = await client.get("/ready")
            data = resp.json()
            assert data["graph"] == "connected"

    @pytest.mark.asyncio
    async def test_ready_graph_disconnected_with_mock_backend(self, client):
        """When graph backend is ready but verify_connectivity returns False."""
        mock_backend = AsyncMock()
        mock_backend.is_ready.return_value = True
        mock_backend.verify_connectivity.return_value = False

        with patch("archolith_proxy.main.is_graph_ready", return_value=True), \
             patch("archolith_proxy.main.get_backend", return_value=mock_backend):
            resp = await client.get("/ready")
            data = resp.json()
            assert data["graph"] == "disconnected"

    @pytest.mark.asyncio
    async def test_ready_graph_verify_raises(self, client):
        """When verify_connectivity raises, graph should be disconnected."""
        mock_backend = AsyncMock()
        mock_backend.is_ready.return_value = True
        mock_backend.verify_connectivity.side_effect = RuntimeError("connection lost")

        with patch("archolith_proxy.main.is_graph_ready", return_value=True), \
             patch("archolith_proxy.main.get_backend", return_value=mock_backend):
            resp = await client.get("/ready")
            data = resp.json()
            assert data["graph"] == "disconnected"


class TestHealthLegacy:
    """Legacy /health endpoint should use backend protocol too."""

    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_health_graph_not_configured(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["graph"] == "not_configured"
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_graph_connected_with_mock(self, client):
        mock_backend = AsyncMock()
        mock_backend.is_ready.return_value = True
        mock_backend.verify_connectivity.return_value = True

        with patch("archolith_proxy.main.is_graph_ready", return_value=True), \
             patch("archolith_proxy.main.get_backend", return_value=mock_backend):
            resp = await client.get("/health")
            data = resp.json()
            assert data["graph"] == "connected"
            assert data["status"] == "ok"
