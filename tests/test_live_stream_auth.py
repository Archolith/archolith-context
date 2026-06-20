"""Tests for the live-stream WebSocket admin boundary."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.datastructures import Headers, QueryParams

from archolith_proxy.routers.live_router import _websocket_authorized


def _ws(host: str | None, query: str = "", headers: dict[str, str] | None = None):
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(
        client=client,
        query_params=QueryParams(query),
        headers=Headers(headers or {}),
    )


def test_empty_admin_token_allows_loopback_websocket():
    assert _websocket_authorized(_ws("127.0.0.1"), admin_token="", allow_open_nonlocal=False) is True


def test_empty_admin_token_rejects_non_loopback_websocket():
    assert _websocket_authorized(_ws("203.0.113.7"), admin_token="", allow_open_nonlocal=False) is False


def test_empty_admin_token_escape_hatch_allows_non_loopback_websocket():
    assert _websocket_authorized(_ws("203.0.113.7"), admin_token="", allow_open_nonlocal=True) is True


def test_admin_token_accepts_query_token():
    assert _websocket_authorized(_ws("203.0.113.7", query="token=secret"), "secret", False) is True


def test_admin_token_accepts_header_token():
    assert _websocket_authorized(_ws("203.0.113.7", headers={"x-admin-token": "secret"}), "secret", False) is True


def test_admin_token_accepts_bearer_token():
    ws = _ws("203.0.113.7", headers={"authorization": "Bearer secret"})
    assert _websocket_authorized(ws, "secret", False) is True


def test_admin_token_rejects_invalid_token_even_from_loopback():
    assert _websocket_authorized(_ws("127.0.0.1", query="token=wrong"), "secret", False) is False
