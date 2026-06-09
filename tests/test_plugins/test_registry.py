"""Tests for PluginRegistry and ProxyPlugin protocol."""

from __future__ import annotations

import pytest

from archolith_proxy.plugins import (
    PluginRegistry,
    ProxyPlugin,
    get_plugin_registry,
    reset_plugin_registry,
)


# ---------------------------------------------------------------------------
# Helpers — minimal protocol implementations
# ---------------------------------------------------------------------------


class _OkPlugin:
    """A well-behaved plugin that activates successfully."""

    def __init__(self, pid: str = "ok", version: str = "1.0.0", metrics: dict | None = None):
        self._id = pid
        self._version = version
        self._metrics = metrics or {}

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
        return {"status": "ok"}

    def contribute_metrics(self) -> dict[str, int | float]:
        return self._metrics


class _DegradedPlugin(_OkPlugin):
    """Plugin whose activate() returns False."""

    async def activate(self) -> bool:
        return False


class _ErrorPlugin(_OkPlugin):
    """Plugin whose activate() raises."""

    async def activate(self) -> bool:
        raise RuntimeError("plugin exploded")


class _BadHealthPlugin(_OkPlugin):
    """Plugin whose healthcheck() raises."""

    async def healthcheck(self) -> dict:
        raise ValueError("health check unavailable")


class _BadMetricsPlugin(_OkPlugin):
    """Plugin whose contribute_metrics() raises."""

    def contribute_metrics(self) -> dict[str, int | float]:
        raise RuntimeError("metrics unavailable")


@pytest.fixture(autouse=True)
def _reset_registry():
    """Ensure a clean PluginRegistry singleton for every test."""
    reset_plugin_registry()
    yield
    reset_plugin_registry()


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_proxy_plugin_is_runtime_checkable():
    """ProxyPlugin is @runtime_checkable — isinstance() works."""
    plugin = _OkPlugin()
    assert isinstance(plugin, ProxyPlugin)


def test_non_compliant_object_fails_isinstance():
    """An object missing protocol methods is not a ProxyPlugin."""
    assert not isinstance(object(), ProxyPlugin)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_adds_plugin():
    r = PluginRegistry()
    r.register(_OkPlugin("a"))
    assert len(r) == 1
    listed = r.list_plugins()
    assert listed[0]["id"] == "a"
    assert listed[0]["status"] == "inactive"


def test_register_replaces_existing_id():
    r = PluginRegistry()
    r.register(_OkPlugin("x", version="1.0.0"))
    r.register(_OkPlugin("x", version="2.0.0"))
    assert len(r) == 1
    assert r.list_plugins()[0]["version"] == "2.0.0"


def test_get_plugin_returns_correct_instance():
    r = PluginRegistry()
    p = _OkPlugin("myplug")
    r.register(p)
    assert r.get_plugin("myplug") is p


def test_get_plugin_unknown_returns_none():
    r = PluginRegistry()
    assert r.get_plugin("nope") is None


# ---------------------------------------------------------------------------
# Lifecycle — activate_all
# ---------------------------------------------------------------------------


async def test_activate_all_success():
    r = PluginRegistry()
    r.register(_OkPlugin("a"))
    r.register(_OkPlugin("b"))
    results = await r.activate_all()
    assert results == {"a": True, "b": True}
    statuses = {p["id"]: p["status"] for p in r.list_plugins()}
    assert statuses["a"] == "active"
    assert statuses["b"] == "active"


async def test_activate_all_degraded_plugin_does_not_crash():
    r = PluginRegistry()
    r.register(_OkPlugin("good"))
    r.register(_DegradedPlugin("bad"))
    results = await r.activate_all()
    assert results["good"] is True
    assert results["bad"] is False
    statuses = {p["id"]: p["status"] for p in r.list_plugins()}
    assert statuses["good"] == "active"
    assert statuses["bad"] == "degraded"


async def test_activate_all_erroring_plugin_does_not_crash():
    r = PluginRegistry()
    r.register(_OkPlugin("good"))
    r.register(_ErrorPlugin("boom"))
    results = await r.activate_all()
    assert results["good"] is True
    assert results["boom"] is False
    statuses = {p["id"]: p["status"] for p in r.list_plugins()}
    assert statuses["good"] == "active"
    assert statuses["boom"] == "error"


async def test_activate_all_with_all_failing_proxy_starts():
    """Even when every plugin fails, activate_all completes (no exception)."""
    r = PluginRegistry()
    r.register(_ErrorPlugin("a"))
    r.register(_DegradedPlugin("b"))
    results = await r.activate_all()
    assert not any(results.values())


async def test_deactivate_all_only_deactivates_active():
    r = PluginRegistry()
    r.register(_OkPlugin("active_plug"))
    r.register(_DegradedPlugin("degraded_plug"))
    await r.activate_all()
    await r.deactivate_all()
    statuses = {p["id"]: p["status"] for p in r.list_plugins()}
    assert statuses["active_plug"] == "inactive"
    # degraded was never active — still degraded (deactivate_all skips it)
    assert statuses["degraded_plug"] == "degraded"


# ---------------------------------------------------------------------------
# Config gating — PLUGINS_ENABLED / PLUGINS_DISABLED
# ---------------------------------------------------------------------------


async def test_plugins_disabled_env_blocks_activation(monkeypatch):
    monkeypatch.setenv("PLUGINS_DISABLED", "filter,audit")
    monkeypatch.delenv("PLUGINS_ENABLED", raising=False)
    r = PluginRegistry()
    r.register(_OkPlugin("filter"))
    r.register(_OkPlugin("memory"))
    results = await r.activate_all()
    assert results["filter"] is False
    assert results["memory"] is True
    statuses = {p["id"]: p["status"] for p in r.list_plugins()}
    assert statuses["filter"] == "inactive"
    assert statuses["memory"] == "active"


async def test_plugins_enabled_env_gates_activation(monkeypatch):
    monkeypatch.setenv("PLUGINS_ENABLED", "memory")
    monkeypatch.delenv("PLUGINS_DISABLED", raising=False)
    r = PluginRegistry()
    r.register(_OkPlugin("filter"))
    r.register(_OkPlugin("memory"))
    results = await r.activate_all()
    assert results["memory"] is True
    assert results["filter"] is False


async def test_plugins_disabled_overrides_enabled(monkeypatch):
    monkeypatch.setenv("PLUGINS_ENABLED", "filter,memory")
    monkeypatch.setenv("PLUGINS_DISABLED", "filter")
    r = PluginRegistry()
    r.register(_OkPlugin("filter"))
    r.register(_OkPlugin("memory"))
    results = await r.activate_all()
    assert results["filter"] is False
    assert results["memory"] is True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def test_healthcheck_ok():
    r = PluginRegistry()
    r.register(_OkPlugin("x"))
    await r.activate_all()
    health = await r.healthcheck("x")
    assert health["status"] == "ok"


async def test_healthcheck_not_found():
    r = PluginRegistry()
    health = await r.healthcheck("nope")
    assert health["status"] == "not_found"


async def test_healthcheck_raising_plugin_returns_unavailable():
    r = PluginRegistry()
    r.register(_BadHealthPlugin("broken"))
    health = await r.healthcheck("broken")
    assert health["status"] == "unavailable"
    assert "error" in health


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_aggregate_metrics_empty():
    r = PluginRegistry()
    assert r.aggregate_metrics() == {}


def test_aggregate_metrics_prefixed():
    r = PluginRegistry()
    r.register(_OkPlugin("filter", metrics={"hits": 5, "misses": 2}))
    r.register(_OkPlugin("audit", metrics={"checks": 10}))
    merged = r.aggregate_metrics()
    assert merged["plugins.filter.hits"] == 5
    assert merged["plugins.filter.misses"] == 2
    assert merged["plugins.audit.checks"] == 10


def test_aggregate_metrics_raising_plugin_omitted():
    """A plugin that raises in contribute_metrics is omitted, others kept."""
    r = PluginRegistry()
    r.register(_OkPlugin("good", metrics={"x": 1}))
    r.register(_BadMetricsPlugin("bad"))
    merged = r.aggregate_metrics()
    assert "plugins.good.x" in merged
    assert not any("bad" in k for k in merged)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_get_plugin_registry_returns_singleton():
    r1 = get_plugin_registry()
    r2 = get_plugin_registry()
    assert r1 is r2


def test_reset_plugin_registry_gives_fresh_instance():
    r1 = get_plugin_registry()
    reset_plugin_registry()
    r2 = get_plugin_registry()
    assert r1 is not r2
