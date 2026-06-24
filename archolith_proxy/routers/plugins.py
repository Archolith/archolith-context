"""Plugin admin endpoints — list plugins and query individual plugin health."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from archolith_proxy.plugins import get_plugin_registry

router = APIRouter()


@router.get("/plugins")
async def list_plugins() -> dict:
    """List all registered plugins with status, version, and count summary."""
    registry = get_plugin_registry()
    plugins = registry.list_plugins()
    active = sum(1 for p in plugins if p["status"] == "active")
    degraded = sum(1 for p in plugins if p["status"] == "degraded")
    error = sum(1 for p in plugins if p["status"] == "error")
    inactive = sum(1 for p in plugins if p["status"] == "inactive")
    return {
        "plugins": plugins,
        "summary": {
            "total": len(plugins),
            "active": active,
            "degraded": degraded,
            "error": error,
            "inactive": inactive,
        },
    }


@router.get("/plugins/audit/report")
async def audit_report() -> dict:
    """Per-server audit usage breakdown (token share + filter savings).

    Complements GET /metrics, which carries only flat audit totals, with the
    per-server detail (which server dominates token usage, and how much the
    filter saves per server). Returns an empty report when the audit plugin is
    absent or there is no telemetry yet — never 500s on missing data.
    """
    empty = {"feed": "none", "servers": [], "totals": {}}
    registry = get_plugin_registry()
    plugin = registry.get_plugin("audit")
    if plugin is None or not hasattr(plugin, "server_report"):
        return empty
    try:
        return plugin.server_report()
    except Exception:
        return empty


@router.get("/plugins/{plugin_id}")
async def get_plugin(plugin_id: str) -> dict:
    """Return detail + live health for a single plugin."""
    registry = get_plugin_registry()
    plugin = registry.get_plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not found")

    health = await registry.healthcheck(plugin_id)
    descriptor = next(
        (p for p in registry.list_plugins() if p["id"] == plugin_id),
        {},
    )
    plugin_prefix = f"plugins.{plugin_id}."
    plugin_metrics = {
        k[len(plugin_prefix):]: v
        for k, v in registry.aggregate_metrics().items()
        if k.startswith(plugin_prefix)
    }
    return {
        "id": plugin_id,
        "version": descriptor.get("version", plugin.plugin_version),
        "status": descriptor.get("status", "unknown"),
        "health": health,
        "metrics": plugin_metrics,
    }
