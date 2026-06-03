"""Tests for the admin token boundary on operator surfaces.

Covers:
- Open access when ADMIN_TOKEN is empty (default)
- 401 rejection with invalid token when ADMIN_TOKEN is set
- 200 acceptance with valid X-Admin-Token header
- 200 acceptance with valid Authorization: Bearer header
- Runtime config override persistence on the admin config endpoints
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from archolith_proxy import config as config_module
from archolith_proxy.config import get_settings, reset_settings


class TestAdminTokenOpen:
    """When ADMIN_TOKEN is empty, all operator endpoints should be open."""

    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
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
        from archolith_proxy.graph.backend import reset_backend
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


class TestAdminConfigPersistence:
    """Runtime config updates should persist to config_overrides.json."""

    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend

        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_patch_persists_override_and_reports_delta(self, client, tmp_path: Path):
        override_file = tmp_path / "config_overrides.json"

        with patch.object(config_module, "_OVERRIDES_FILE", override_file):
            reset_settings()

            resp = await client.patch(
                "/admin/config",
                json={"context_token_budget": 12345},
            )

            assert resp.status_code == 200
            body = resp.json()
            assert body["updated"]["context_token_budget"] == 12345
            assert "persist" in body["warning"].lower()
            assert override_file.exists()

            delta_resp = await client.get("/admin/config-delta")
            assert delta_resp.status_code == 200
            delta = delta_resp.json()
            assert "context_token_budget" in delta["overridden"]
            assert delta["current"]["context_token_budget"] == 12345

            reset_settings()
            assert get_settings().context_token_budget == 12345
