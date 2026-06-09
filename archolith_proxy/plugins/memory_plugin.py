"""MemoryPlugin — wraps the memory engine registry via the ProxyPlugin contract.

Provides an observability surface for the existing memory integration without
changing its initialization path. The MemoryEngineRegistry is still populated
by main.py's lifespan; this plugin reads the populated registry to expose
health and metrics through the standard plugin contract.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

_PLUGIN_ID = "memory"


class MemoryPlugin:
    """ProxyPlugin implementation for archolith-memory engines.

    activate() → confirms the memory registry is accessible and at least
    one engine is configured. Returns True if promotion is enabled with
    engines registered, False otherwise (no engines configured is not an error).

    contribute_metrics() → reads the process-level MemoryEngineRegistry and
    the proxy's _metrics dict for promotion counters.
    """

    @property
    def plugin_id(self) -> str:
        return _PLUGIN_ID

    @property
    def plugin_version(self) -> str:
        try:
            import archolith_proxy
            return getattr(archolith_proxy, "__version__", "unknown")
        except ImportError:
            return "unknown"

    async def activate(self) -> bool:
        """Check that the memory registry is accessible."""
        try:
            from archolith_proxy.memory.registry import get_registry
            registry = get_registry()
            engine_count = registry.engine_count
            if engine_count > 0:
                logger.info("memory_plugin_activated", engines=engine_count)
            else:
                logger.info(
                    "memory_plugin_activated_no_engines",
                    note="no memory engines configured — promotion disabled",
                )
            # Always return True — absence of engines is not a failure
            return True
        except Exception as exc:
            logger.warning("memory_plugin_activate_error", error=str(exc))
            return False

    async def deactivate(self) -> None:
        """Adapters are closed by main.py lifespan shutdown — no-op here."""
        pass

    async def healthcheck(self) -> dict:
        """Report per-engine health (instantiated adapters only)."""
        try:
            from archolith_proxy.memory.registry import get_registry
            registry = get_registry()
            engines = registry.list_engines()
            return {
                "status": "ok",
                "engines_total": registry.engine_count,
                "engines": engines,
            }
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}

    def contribute_metrics(self) -> dict[str, int | float]:
        """Return engine count and promotion counters from process metrics."""
        try:
            from archolith_proxy.memory.registry import get_registry
            from archolith_proxy.metrics import get_metrics

            registry = get_registry()
            m = get_metrics()
            return {
                "engines_configured": registry.engine_count,
                "promotions_attempted": m.get("promotions_attempted", 0),
                "promotions_succeeded": m.get("promotions_succeeded", 0),
                "promotions_failed": m.get("promotions_failed", 0),
                "promotions_skipped": m.get("promotions_skipped", 0),
            }
        except Exception:
            return {}
