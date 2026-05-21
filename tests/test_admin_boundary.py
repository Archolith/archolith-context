"""Tests for the admin token boundary on operator surfaces.

Covers:
- Open access when ADMIN_TOKEN is empty (default)
- 401 rejection with invalid token when ADMIN_TOKEN is set
- 200 acceptance with valid X-Admin-Token header
- 200 acceptance with valid Authorization: Bearer header
"""

from unittest.mock import patch

import pytest

from src.config import reset_settings


class TestAdminTokenOpen:
    """When ADMIN_TOKEN is empty, all operator endpoints should be open."""

    def setup_method(self):
        from src.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_sessions_open_when_no_token(self, client):
        """Sessions endpoint returns 503 (no graph), not 401."""
        resp = await client.get("/sessions")
        # 503 because graph isn't configured, but NOT 401
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_promotions_open_when_no_token(self, client):
        """Promotions endpoint returns 503 (no service), not 401."""
        resp = await client.get("/promotions")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_memory_engines_open_when_no_token(self, client):
        """Memory engines endpoint accessible without token."""
        resp = await client.get("/memory-engines")
        assert resp.status_code == 200


class TestAdminTokenEnforced:
    """When ADMIN_TOKEN is set, operator endpoints require valid token."""

    def setup_method(self):
        from src.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_sessions_rejected_without_token(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get("/sessions")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sessions_rejected_with_wrong_token(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get(
                "/sessions",
                headers={"X-Admin-Token": "wrong-token"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sessions_accepted_with_x_admin_token(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get(
                "/sessions",
                headers={"X-Admin-Token": "secret-test-token"},
            )
            # 503 because graph isn't configured, but NOT 401
            assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_sessions_accepted_with_bearer(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get(
                "/sessions",
                headers={"Authorization": "Bearer secret-test-token"},
            )
            assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_promotions_rejected_without_token(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get("/promotions")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memory_engines_rejected_without_token(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get("/memory-engines")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_memory_engines_accepted_with_token(self, client):
        with patch.dict("os.environ", {"ADMIN_TOKEN": "secret-test-token"}):
            reset_settings()
            resp = await client.get(
                "/memory-engines",
                headers={"X-Admin-Token": "secret-test-token"},
            )
            assert resp.status_code == 200
