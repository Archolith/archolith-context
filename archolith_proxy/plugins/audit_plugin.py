"""AuditPlugin — wraps archolith-mcp-audit via the ProxyPlugin contract.

Provides a lightweight in-proxy surface for archolith-audit:
- activate() confirms the package is importable
- healthcheck() reports package version
- contribute_metrics() reads LiveAccumulator totals when available

The LiveAccumulator is wired into the proxy's extraction pipeline separately
(see archolith_mcp_audit.accumulator). This plugin provides the registry
lifecycle surface and the metrics aggregation — it does not feed the
accumulator itself.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

_PLUGIN_ID = "audit"


class AuditPlugin:
    """ProxyPlugin implementation for archolith-mcp-audit.

    activate() → confirms archolith_mcp_audit is importable. Returns False when
    the package is not installed; proxy still starts normally.

    contribute_metrics() → reads the global LiveAccumulator if one is attached
    to app state (set by proxy startup code). Falls back to empty dict.
    """

    def __init__(self) -> None:
        # LiveAccumulator reference, set externally after app startup
        self._accumulator = None

    @property
    def plugin_id(self) -> str:
        return _PLUGIN_ID

    @property
    def plugin_version(self) -> str:
        try:
            import archolith_mcp_audit
            return getattr(archolith_mcp_audit, "__version__", "unknown")
        except ImportError:
            return "not_installed"

    async def activate(self) -> bool:
        """Confirm archolith_mcp_audit is importable."""
        try:
            import archolith_mcp_audit  # noqa: F401
            logger.info("audit_plugin_activated", version=self.plugin_version)
            return True
        except ImportError:
            logger.warning(
                "audit_plugin_unavailable",
                note="archolith_mcp_audit not installed — audit features disabled",
            )
            return False
        except Exception as exc:
            logger.warning("audit_plugin_activate_error", error=str(exc))
            return False

    async def deactivate(self) -> None:
        """Release accumulator reference."""
        self._accumulator = None

    async def healthcheck(self) -> dict:
        """Report audit package availability and accumulator state."""
        try:
            import archolith_mcp_audit  # noqa: F401
            accumulator_active = self._accumulator is not None
            return {
                "status": "ok",
                "version": self.plugin_version,
                "accumulator_active": accumulator_active,
            }
        except ImportError:
            return {"status": "unavailable", "version": "not_installed"}
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}

    def set_accumulator(self, accumulator) -> None:
        """Attach a LiveAccumulator instance for metrics reporting."""
        self._accumulator = accumulator

    def contribute_metrics(self) -> dict[str, int | float]:
        """Return aggregate stats from the attached LiveAccumulator."""
        if self._accumulator is None:
            return {}
        try:
            acc = self._accumulator
            return {
                "total_results": acc.total_results,
                "total_raw_chars": acc.total_raw_chars,
                "total_filtered_chars": acc.total_filtered_chars,
                "servers_seen": len(acc.servers),
            }
        except Exception:
            return {}
