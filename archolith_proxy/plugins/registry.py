"""PluginRegistry and ProxyPlugin protocol for archolith-proxy.

The ProxyPlugin protocol is the contract every plugin must satisfy.
The PluginRegistry manages lifecycle (activate / deactivate), health,
and metrics aggregation for all registered plugins.
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol, runtime_checkable

import structlog

logger = structlog.get_logger()

# Minimum compatible version for each built-in plugin.
# Activation fails (plugin marked 'error') when installed version is below this.
# Proxy still starts — version mismatch is non-fatal.
MIN_PLUGIN_VERSIONS: dict[str, str] = {
    "filter": "0.1.0",
    "audit": "0.1.0",
    "memory": "0.1.0",
}


def _version_meets_minimum(installed: str, minimum: str) -> bool:
    """Return True if installed version >= minimum using PEP 440 comparison."""
    try:
        from packaging.version import Version
        return Version(installed) >= Version(minimum)
    except Exception:
        # Fall back to lexicographic comparison when packaging is unavailable.
        # Adequate for simple x.y.z versions.
        return installed >= minimum


@runtime_checkable
class ProxyPlugin(Protocol):
    """Contract for modules that plug into archolith-proxy."""

    @property
    def plugin_id(self) -> str:
        """Unique identifier (e.g. 'filter', 'audit', 'memory')."""
        ...

    @property
    def plugin_version(self) -> str:
        """Semantic version of the plugin."""
        ...

    async def activate(self) -> bool:
        """Called once at proxy startup. Return True if ready."""
        ...

    async def deactivate(self) -> None:
        """Called at proxy shutdown. Best-effort cleanup."""
        ...

    async def healthcheck(self) -> dict:
        """Return {'status': 'ok'|'degraded'|'unavailable', ...}."""
        ...

    def contribute_metrics(self) -> dict[str, int | float]:
        """Return flat dict of plugin-specific counters for GET /metrics.

        Called by the metrics endpoint; should not block.
        """
        ...


class PluginRegistry:
    """Lifecycle manager and metrics aggregator for ProxyPlugin instances.

    Fail-safe contract:
    - activate() returning False  → plugin listed as 'degraded', proxy starts normally
    - activate() raising          → caught, logged, plugin listed as 'error', proxy starts normally
    - healthcheck() raising       → caught, status reported as 'unavailable'
    - contribute_metrics() raising → caught, plugin's metrics omitted from that poll

    The proxy **never** fails to start because a plugin misbehaves.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, ProxyPlugin] = {}
        self._statuses: dict[str, str] = {}  # 'active', 'degraded', 'error', 'inactive'

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, plugin: ProxyPlugin) -> None:
        """Register a plugin. Replaces any existing plugin with the same ID."""
        pid = plugin.plugin_id
        self._plugins[pid] = plugin
        self._statuses[pid] = "inactive"
        logger.debug("plugin_registered", plugin_id=pid, version=plugin.plugin_version)

    def get_plugin(self, plugin_id: str) -> ProxyPlugin | None:
        """Return the plugin with the given ID, or None."""
        return self._plugins.get(plugin_id)

    def list_plugins(self) -> list[dict]:
        """Return a list of plugin descriptors: id, version, status."""
        return [
            {
                "id": pid,
                "version": plugin.plugin_version,
                "status": self._statuses.get(pid, "inactive"),
            }
            for pid, plugin in self._plugins.items()
        ]

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _is_enabled(self, plugin_id: str) -> bool:
        """Return True if this plugin should be activated per PLUGINS_ENABLED/DISABLED env vars."""
        enabled_env = os.environ.get("PLUGINS_ENABLED", "").strip()
        disabled_env = os.environ.get("PLUGINS_DISABLED", "").strip()

        disabled_ids = {s.strip() for s in disabled_env.split(",") if s.strip()}
        if plugin_id in disabled_ids:
            return False

        if enabled_env:
            enabled_ids = {s.strip() for s in enabled_env.split(",") if s.strip()}
            return plugin_id in enabled_ids

        # PLUGINS_ENABLED is empty → all installed plugins enabled by default
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def activate_all(self) -> dict[str, bool]:
        """Activate all registered plugins. Returns {plugin_id: success}.

        Plugins disabled by config are skipped (status='inactive').
        Plugins that raise or return False are marked degraded/error but do
        not prevent the proxy from starting.
        """
        results: dict[str, bool] = {}
        for pid, plugin in self._plugins.items():
            if not self._is_enabled(pid):
                self._statuses[pid] = "inactive"
                logger.info("plugin_skipped_disabled", plugin_id=pid)
                results[pid] = False
                continue

            try:
                # Version compatibility check before activation
                min_ver = MIN_PLUGIN_VERSIONS.get(pid)
                if min_ver:
                    installed_ver = plugin.plugin_version
                    if installed_ver not in ("unknown", "not_installed") and not _version_meets_minimum(installed_ver, min_ver):
                        self._statuses[pid] = "error"
                        logger.error(
                            "plugin_version_incompatible",
                            plugin_id=pid,
                            installed=installed_ver,
                            required=min_ver,
                            hint=f"pip install archolith-proxy[{pid}] to upgrade",
                        )
                        results[pid] = False
                        continue

                ok = await asyncio.wait_for(plugin.activate(), timeout=10.0)
                if ok:
                    self._statuses[pid] = "active"
                    logger.info("plugin_activated", plugin_id=pid, version=plugin.plugin_version)
                else:
                    self._statuses[pid] = "degraded"
                    logger.warning("plugin_activate_returned_false", plugin_id=pid)
                results[pid] = bool(ok)
            except Exception as exc:
                self._statuses[pid] = "error"
                logger.warning("plugin_activate_error", plugin_id=pid, error=str(exc))
                results[pid] = False

        return results

    async def deactivate_all(self) -> None:
        """Deactivate all active plugins. Best-effort — exceptions are swallowed."""
        for pid, plugin in self._plugins.items():
            if self._statuses.get(pid) != "active":
                continue
            try:
                await asyncio.wait_for(plugin.deactivate(), timeout=5.0)
                self._statuses[pid] = "inactive"
                logger.info("plugin_deactivated", plugin_id=pid)
            except Exception as exc:
                logger.warning("plugin_deactivate_error", plugin_id=pid, error=str(exc))

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def healthcheck(self, plugin_id: str) -> dict:
        """Run healthcheck for a single plugin. Returns {'status': ..., ...}."""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return {"status": "not_found"}
        try:
            return await asyncio.wait_for(plugin.healthcheck(), timeout=5.0)
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def aggregate_metrics(self) -> dict[str, int | float]:
        """Return merged plugin metrics under 'plugins.<id>.*' keys."""
        merged: dict[str, int | float] = {}
        for pid, plugin in self._plugins.items():
            try:
                plugin_metrics = plugin.contribute_metrics()
                for k, v in plugin_metrics.items():
                    merged[f"plugins.{pid}.{k}"] = v
            except Exception:
                pass
        return merged

    def __len__(self) -> int:
        return len(self._plugins)
