"""Tests for the D1 admin loopback guard.

When ADMIN_TOKEN is empty, admin access is allowed only from loopback peers
(unless ADMIN_ALLOW_OPEN_NONLOCAL is set). When set, token rules apply as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from archolith_proxy.admin import _is_loopback, require_admin_token


def _req(host: str | None, headers: dict | None = None):
    """Minimal stand-in for a FastAPI Request: .client.host and .headers."""
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(client=client, headers=headers or {})


def _settings(admin_token: str = "", admin_allow_open_nonlocal: bool = False):
    return SimpleNamespace(
        admin_token=admin_token,
        admin_allow_open_nonlocal=admin_allow_open_nonlocal,
    )


# ── _is_loopback ────────────────────────────────────────────────────────────

class TestIsLoopback:
    def test_ipv4_loopback(self):
        assert _is_loopback("127.0.0.1") is True
        assert _is_loopback("127.0.0.5") is True

    def test_ipv6_loopback(self):
        assert _is_loopback("::1") is True

    def test_ipv4_mapped_ipv6_loopback(self):
        assert _is_loopback("::ffff:127.0.0.1") is True

    def test_non_loopback_ipv4(self):
        assert _is_loopback("10.0.0.4") is False
        assert _is_loopback("192.168.1.10") is False

    def test_hostname_is_not_loopback(self):
        # 'localhost' is a name, not a parseable address -> fail closed.
        assert _is_loopback("localhost") is False

    def test_none_and_garbage(self):
        assert _is_loopback(None) is False
        assert _is_loopback("") is False
        assert _is_loopback("not-an-ip") is False


# ── require_admin_token (empty token) ───────────────────────────────────────

class TestEmptyTokenLoopbackGuard:
    @pytest.mark.asyncio
    async def test_empty_token_allows_loopback(self):
        with patch("archolith_proxy.admin.get_settings", return_value=_settings()):
            # Must not raise.
            await require_admin_token(_req("127.0.0.1"))

    @pytest.mark.asyncio
    async def test_empty_token_denies_non_loopback(self):
        with patch("archolith_proxy.admin.get_settings", return_value=_settings()):
            with pytest.raises(HTTPException) as exc:
                await require_admin_token(_req("203.0.113.7"))
            assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_token_denies_when_no_client(self):
        with patch("archolith_proxy.admin.get_settings", return_value=_settings()):
            with pytest.raises(HTTPException):
                await require_admin_token(_req(None))

    @pytest.mark.asyncio
    async def test_open_nonlocal_escape_hatch_allows_any_peer(self):
        s = _settings(admin_allow_open_nonlocal=True)
        with patch("archolith_proxy.admin.get_settings", return_value=s):
            await require_admin_token(_req("203.0.113.7"))  # must not raise


# ── require_admin_token (token set) — unchanged enforcement ─────────────────

class TestTokenSetStillEnforced:
    @pytest.mark.asyncio
    async def test_valid_x_admin_token_from_non_loopback(self):
        s = _settings(admin_token="secret")
        with patch("archolith_proxy.admin.get_settings", return_value=s):
            await require_admin_token(_req("203.0.113.7", {"x-admin-token": "secret"}))

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self):
        s = _settings(admin_token="secret")
        with patch("archolith_proxy.admin.get_settings", return_value=s):
            await require_admin_token(_req("203.0.113.7", {"authorization": "Bearer secret"}))

    @pytest.mark.asyncio
    async def test_invalid_token_rejected_even_from_loopback(self):
        s = _settings(admin_token="secret")
        with patch("archolith_proxy.admin.get_settings", return_value=s):
            with pytest.raises(HTTPException) as exc:
                await require_admin_token(_req("127.0.0.1", {"x-admin-token": "wrong"}))
            assert exc.value.status_code == 401
