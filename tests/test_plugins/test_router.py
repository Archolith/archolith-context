"""HTTP-level tests for GET /plugins and GET /plugins/{id}."""

from __future__ import annotations

import pytest

from archolith_proxy.plugins import get_plugin_registry, reset_plugin_registry


class _OkPlugin:
    def __init__(self, pid: str, version: str = "1.0.0"):
        self._id = pid
        self._version = version

    @property
    def plugin_id(self) -> str:
        return self._id

    @property
    def plugin_version(self) -> str:
        return self._version

    async def activate(self) -> bool:
        return True

    async def deactivate(self) -> None:
        pass

    async def healthcheck(self) -> dict:
        return {"status": "ok", "plugin_id": self._id}

    def contribute_metrics(self) -> dict[str, int | float]:
        return {"calls": 42}


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_plugin_registry()
    yield
    reset_plugin_registry()


@pytest.fixture
def registered_client(client):
    """An httpx test client with two plugins pre-registered."""
    registry = get_plugin_registry()
    registry.register(_OkPlugin("filter", "0.5.0"))
    registry.register(_OkPlugin("audit", "0.2.0"))
    return client


async def test_list_plugins_empty(client):
    resp = await client.get("/plugins")
    assert resp.status_code == 200
    body = resp.json()
    assert body["plugins"] == []
    assert body["summary"]["total"] == 0


async def test_list_plugins_returns_registered(registered_client):
    resp = await registered_client.get("/plugins")
    assert resp.status_code == 200
    body = resp.json()
    ids = {p["id"] for p in body["plugins"]}
    assert ids == {"filter", "audit"}
    assert body["summary"]["total"] == 2


async def test_list_plugins_summary_counts(registered_client):
    """Summary counts reflect inactive status before activation."""
    resp = await registered_client.get("/plugins")
    body = resp.json()
    assert body["summary"]["active"] == 0
    assert body["summary"]["inactive"] == 2


async def test_get_plugin_returns_detail(registered_client):
    resp = await registered_client.get("/plugins/filter")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "filter"
    assert body["version"] == "0.5.0"
    assert "status" in body
    assert "health" in body


async def test_get_plugin_health_ok(registered_client):
    resp = await registered_client.get("/plugins/filter")
    body = resp.json()
    assert body["health"]["status"] == "ok"


async def test_get_plugin_metrics(registered_client):
    """Plugin metrics are returned under 'metrics' key."""
    resp = await registered_client.get("/plugins/filter")
    body = resp.json()
    assert body["metrics"].get("calls") == 42


async def test_get_plugin_not_found(client):
    resp = await client.get("/plugins/nonexistent")
    assert resp.status_code == 404
