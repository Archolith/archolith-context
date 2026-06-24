"""AuditPlugin — wraps archolith-mcp-audit via the ProxyPlugin contract.

Provides a lightweight in-proxy surface for archolith-audit:
- activate() confirms the package is importable
- healthcheck() reports package version and metrics feed state
- contribute_metrics() reports per-server token usage totals

Metrics feed:
- An explicit LiveAccumulator may be attached via set_accumulator() (used by
  tests and any external feeder); when present, its totals are reported as-is.
- Otherwise, when the lazy filter feed is enabled at startup via
  enable_filter_feed() and both archolith_filter and archolith_mcp_audit are
  installed, contribute_metrics() builds a fresh LiveAccumulator on demand from
  archolith-filter's FilterTelemetryStore singleton (the same store the
  FilterPlugin reports on). This is a pull-based, read-only feed computed on
  each /metrics poll — it never touches the request path and never mutates the
  telemetry store.
- With no attached accumulator and the lazy feed disabled (or no activity),
  contribute_metrics() returns {} and healthcheck() reports feed='none'.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

_PLUGIN_ID = "audit"


class AuditPlugin:
    """ProxyPlugin implementation for archolith-mcp-audit.

    activate() → confirms archolith_mcp_audit is importable. Returns False when
    the package is not installed; proxy still starts normally.

    contribute_metrics() → reports LiveAccumulator totals. An explicitly
    attached accumulator (set_accumulator) takes precedence; otherwise, when the
    lazy filter feed is enabled, totals are computed on demand from
    archolith-filter telemetry. Falls back to an empty dict.
    """

    def __init__(self) -> None:
        # LiveAccumulator reference, set externally via set_accumulator().
        self._accumulator = None
        # Lazy filter-telemetry feed gate; enabled at startup by enable_filter_feed().
        self._live_feed_enabled = False

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
        """Release accumulator reference and disable the lazy feed."""
        self._accumulator = None
        self._live_feed_enabled = False

    async def healthcheck(self) -> dict:
        """Report audit package availability and metrics feed state."""
        try:
            import archolith_mcp_audit  # noqa: F401
            accumulator_active = self._accumulator is not None
            return {
                "status": "ok",
                "version": self.plugin_version,
                "accumulator_active": accumulator_active,
                "feed": self._feed_state(),
            }
        except ImportError:
            return {"status": "unavailable", "version": "not_installed"}
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}

    def set_accumulator(self, accumulator) -> None:
        """Attach a LiveAccumulator instance for metrics reporting."""
        self._accumulator = accumulator

    def enable_filter_feed(self) -> bool:
        """Enable the lazy filter-telemetry feed (called once at proxy startup).

        Returns True when both archolith_filter and archolith_mcp_audit are
        importable, so contribute_metrics() can compute totals on demand from
        the filter telemetry store. No-op (returns False) otherwise.
        """
        if not self._filter_feed_available():
            return False
        self._live_feed_enabled = True
        logger.info("audit_plugin_filter_feed_enabled")
        return True

    def _feed_state(self) -> str:
        """Return the current metrics feed state: 'live' | 'lazy' | 'none'."""
        if self._accumulator is not None:
            return "live"
        if self._live_feed_enabled and self._filter_feed_available():
            return "lazy"
        return "none"

    @staticmethod
    def _filter_feed_available() -> bool:
        """True when the lazy filter feed's dependencies are importable."""
        try:
            import archolith_filter.telemetry  # noqa: F401
            import archolith_mcp_audit.accumulator  # noqa: F401
            return True
        except ImportError:
            return False

    def _live_accumulator_from_filter(self):
        """Build a fresh LiveAccumulator from archolith-filter telemetry.

        Reads the FilterTelemetryStore singleton (the same one FilterPlugin
        reports on) and replays its entries into a new LiveAccumulator.
        Read-only: never mutates the store. Returns None when unavailable.
        """
        try:
            from archolith_filter.telemetry import get_filter_telemetry_store
            from archolith_mcp_audit.accumulator import LiveAccumulator
        except ImportError:
            return None
        try:
            store = get_filter_telemetry_store()
            acc = LiveAccumulator()
            for entry in store.entries:
                acc.observe(
                    getattr(entry, "tool", None) or "unknown",
                    getattr(entry, "raw_chars", 0),
                    getattr(entry, "filtered_chars", 0),
                )
            return acc
        except Exception:
            return None

    @staticmethod
    def _accumulator_totals(acc) -> dict[str, int | float]:
        """Extract reportable totals from a LiveAccumulator."""
        try:
            return {
                "total_results": acc.total_results,
                "total_raw_chars": acc.total_raw_chars,
                "total_filtered_chars": acc.total_filtered_chars,
                "servers_seen": len(acc.servers),
            }
        except Exception:
            return {}

    def contribute_metrics(self) -> dict[str, int | float]:
        """Return aggregate per-server token totals.

        An explicitly attached accumulator takes precedence and is reported
        as-is. Otherwise, when the lazy filter feed is enabled, totals are
        computed on demand from filter telemetry (suppressed when there is no
        activity). Returns {} when no source is available.
        """
        if self._accumulator is not None:
            return self._accumulator_totals(self._accumulator)
        if self._live_feed_enabled:
            acc = self._live_accumulator_from_filter()
            if acc is not None and acc.total_results > 0:
                return self._accumulator_totals(acc)
        return {}

    def _resolve_accumulator(self):
        """Return the accumulator to report from: attached, else lazy feed."""
        if self._accumulator is not None:
            return self._accumulator
        if self._live_feed_enabled:
            return self._live_accumulator_from_filter()
        return None

    def server_report(self) -> dict:
        """Return a per-server usage/savings breakdown for the report endpoint.

        Uses the same source as contribute_metrics() (attached accumulator,
        else the lazy filter feed), but exposes the per-server detail that the
        flat /metrics totals drop: call count, token share, filter savings, and
        the tools seen per server. On the proxy live path the savings are real
        (archolith-filter telemetry carries true filtered counts).

        NOTE: this is a usage breakdown, NOT the offline audit tool's
        waste-pattern report — detector findings (polling waste, oversized
        results, schema cost, ...) require a pass over session logs via the
        audit CLI and are intentionally not included here.

        Returns {"feed", "servers": [...], "totals": {...}}; an empty report
        (no servers) when there is no telemetry or no source attached.
        """
        feed = self._feed_state()
        acc = self._resolve_accumulator()
        if acc is None or getattr(acc, "total_results", 0) == 0:
            return {"feed": feed, "servers": [], "totals": {}}
        summary = acc.get_server_summary()
        servers = [
            {
                "server": name,
                "call_count": data["call_count"],
                "raw_chars": data["raw_chars"],
                "share_pct": data["share_pct"],
                "savings_pct": data["savings_pct"],
                "tools": data["tools"],
            }
            for name, data in summary.items()
        ]
        return {
            "feed": feed,
            "servers": servers,
            "totals": self._accumulator_totals(acc),
        }
