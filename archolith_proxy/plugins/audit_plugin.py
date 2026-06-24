"""AuditPlugin — wraps archolith-mcp-audit via the ProxyPlugin contract.

Provides a lightweight in-proxy surface for archolith-audit:
- activate() confirms the package is importable
- healthcheck() reports package version and accumulator state
- contribute_metrics() reads LiveAccumulator totals when available

NOTE: No live metrics feed is currently wired in proxy runtime. set_accumulator()
exists for attaching a LiveAccumulator, but nothing in the proxy calls it yet
(only tests do). Until a feed is attached, contribute_metrics() returns {} and
healthcheck() reports feed='none'.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

_PLUGIN_ID = "audit"


class AuditPlugin:
    """ProxyPlugin implementation for archolith-mcp-audit.

    activate() → confirms archolith_mcp_audit is importable. Returns False when
    the package is not installed; proxy still starts normally.

    contribute_metrics() → reads the global LiveAccumulator only if one has been
    attached via set_accumulator(). No runtime caller attaches one yet (only tests
    do). Falls back to empty dict when accumulator is None.
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
                "feed": "live" if self._accumulator is not None else "none",
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
