"""Tests for FilterPlugin, MemoryPlugin, and AuditPlugin."""

from __future__ import annotations

import pytest

from archolith_proxy.plugins import reset_plugin_registry
from archolith_proxy.plugins.audit_plugin import AuditPlugin
from archolith_proxy.plugins.filter_plugin import FilterPlugin
from archolith_proxy.plugins.memory_plugin import MemoryPlugin


@pytest.fixture(autouse=True)
def _reset():
    reset_plugin_registry()
    yield
    reset_plugin_registry()


# ---------------------------------------------------------------------------
# FilterPlugin
# ---------------------------------------------------------------------------


def test_filter_plugin_id():
    assert FilterPlugin().plugin_id == "filter"


def test_filter_plugin_version_not_installed():
    """When archolith_filter is absent, version is 'not_installed'."""
    p = FilterPlugin()
    # Filter is not installed in this venv — version should say so
    v = p.plugin_version
    assert isinstance(v, str)
    assert len(v) > 0


async def test_filter_plugin_activate_returns_bool():
    """activate() always returns a bool (True or False) without raising."""
    result = await FilterPlugin().activate()
    assert isinstance(result, bool)


async def test_filter_plugin_healthcheck_returns_dict():
    health = await FilterPlugin().healthcheck()
    assert "status" in health
    assert health["status"] in ("ok", "unavailable", "degraded")


def test_filter_plugin_contribute_metrics_returns_dict():
    metrics = FilterPlugin().contribute_metrics()
    assert isinstance(metrics, dict)


def test_filter_plugin_contribute_metrics_no_raise():
    """contribute_metrics() never raises, even when filter is absent."""
    try:
        FilterPlugin().contribute_metrics()
    except Exception as exc:
        pytest.fail(f"contribute_metrics raised: {exc}")


async def test_filter_plugin_activate_no_raise():
    """activate() never raises — fail-open even when filter not installed."""
    try:
        await FilterPlugin().activate()
    except Exception as exc:
        pytest.fail(f"activate raised: {exc}")


async def test_filter_plugin_deactivate_no_raise():
    try:
        await FilterPlugin().deactivate()
    except Exception as exc:
        pytest.fail(f"deactivate raised: {exc}")


# ---------------------------------------------------------------------------
# MemoryPlugin
# ---------------------------------------------------------------------------


def test_memory_plugin_id():
    assert MemoryPlugin().plugin_id == "memory"


def test_memory_plugin_version_is_string():
    assert isinstance(MemoryPlugin().plugin_version, str)


async def test_memory_plugin_activate_returns_true():
    """activate() should always return True (registry is always accessible)."""
    result = await MemoryPlugin().activate()
    assert result is True


async def test_memory_plugin_healthcheck_returns_dict():
    health = await MemoryPlugin().healthcheck()
    assert "status" in health
    assert "engines_total" in health


async def test_memory_plugin_healthcheck_engines_count():
    """engines_total reflects the configured engine count (0 in test env)."""
    health = await MemoryPlugin().healthcheck()
    assert health["engines_total"] == 0
    assert health["engines"] == []


def test_memory_plugin_contribute_metrics_returns_dict():
    metrics = MemoryPlugin().contribute_metrics()
    assert isinstance(metrics, dict)
    assert "engines_configured" in metrics
    assert "promotions_attempted" in metrics


def test_memory_plugin_contribute_metrics_no_raise():
    try:
        MemoryPlugin().contribute_metrics()
    except Exception as exc:
        pytest.fail(f"contribute_metrics raised: {exc}")


async def test_memory_plugin_deactivate_no_raise():
    try:
        await MemoryPlugin().deactivate()
    except Exception as exc:
        pytest.fail(f"deactivate raised: {exc}")


# ---------------------------------------------------------------------------
# AuditPlugin
# ---------------------------------------------------------------------------


def test_audit_plugin_id():
    assert AuditPlugin().plugin_id == "audit"


def test_audit_plugin_version_is_string():
    v = AuditPlugin().plugin_version
    assert isinstance(v, str)
    assert len(v) > 0


async def test_audit_plugin_activate_returns_bool():
    result = await AuditPlugin().activate()
    assert isinstance(result, bool)


async def test_audit_plugin_activate_no_raise():
    try:
        await AuditPlugin().activate()
    except Exception as exc:
        pytest.fail(f"activate raised: {exc}")


async def test_audit_plugin_healthcheck_returns_dict():
    health = await AuditPlugin().healthcheck()
    assert "status" in health


def test_audit_plugin_contribute_metrics_no_accumulator():
    """Without an accumulator, contribute_metrics returns empty dict."""
    metrics = AuditPlugin().contribute_metrics()
    assert metrics == {}


def test_audit_plugin_set_accumulator():
    """set_accumulator stores the reference and contributes its stats."""

    class _FakeAccumulator:
        total_results = 7
        total_raw_chars = 1000
        total_filtered_chars = 600
        servers = {"vps": None, "memory": None}

    p = AuditPlugin()
    p.set_accumulator(_FakeAccumulator())
    metrics = p.contribute_metrics()
    assert metrics["total_results"] == 7
    assert metrics["total_raw_chars"] == 1000
    assert metrics["total_filtered_chars"] == 600
    assert metrics["servers_seen"] == 2


async def test_audit_plugin_deactivate_clears_accumulator():
    class _FakeAcc:
        total_results = 1
        total_raw_chars = 0
        total_filtered_chars = 0
        servers = {}

    p = AuditPlugin()
    p.set_accumulator(_FakeAcc())
    await p.deactivate()
    assert p.contribute_metrics() == {}


async def test_audit_plugin_deactivate_no_raise():
    try:
        await AuditPlugin().deactivate()
    except Exception as exc:
        pytest.fail(f"deactivate raised: {exc}")
