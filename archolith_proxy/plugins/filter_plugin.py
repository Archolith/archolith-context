"""FilterPlugin — wraps archolith-filter via the ProxyPlugin contract.

Wraps the existing filter_adapter.py sentinel-based integration without
changing its public API. Adds a lifecycle surface (activate/deactivate)
and exposes FilterTelemetryStore stats through contribute_metrics().

The proxy continues to function in passthrough mode when filter is absent
or when this plugin is disabled — fail-open is unchanged.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

_PLUGIN_ID = "filter"


class FilterPlugin:
    """ProxyPlugin implementation for archolith-filter.

    activate() → forces early resolution of the filter sentinels so the
    first request does not pay the import cost. Returns False when the
    archolith_filter package is not installed; proxy still starts normally.

    contribute_metrics() → reads FilterTelemetryStore (process-level
    singleton from archolith_filter.telemetry).  Returns zeros when filter
    is absent.
    """

    @property
    def plugin_id(self) -> str:
        return _PLUGIN_ID

    @property
    def plugin_version(self) -> str:
        try:
            import archolith_filter
            return getattr(archolith_filter, "__version__", "unknown")
        except ImportError:
            return "not_installed"

    async def activate(self) -> bool:
        """Force eager resolution of filter sentinels. Returns True if filter is callable."""
        try:
            from archolith_proxy.filter_adapter import is_available, _load_filter_output, _load_shrink_functions

            # Force both lazy-load paths to resolve at startup
            _load_filter_output()
            _load_shrink_functions()

            if is_available():
                logger.info("filter_plugin_activated", version=self.plugin_version)
                return True
            else:
                logger.warning(
                    "filter_plugin_degraded",
                    note="archolith_filter not importable — proxy runs in passthrough mode",
                )
                return False
        except Exception as exc:
            logger.warning("filter_plugin_activate_error", error=str(exc))
            return False

    async def deactivate(self) -> None:
        """No-op — filter module cannot be unloaded once imported."""
        pass

    async def healthcheck(self) -> dict:
        """Check whether the filter function is still callable."""
        try:
            from archolith_proxy.filter_adapter import is_available
            available = is_available()
            return {
                "status": "ok" if available else "unavailable",
                "filter_callable": available,
                "version": self.plugin_version,
            }
        except Exception as exc:
            return {"status": "unavailable", "error": str(exc)}

    def contribute_metrics(self) -> dict[str, int | float]:
        """Return aggregate stats from FilterTelemetryStore."""
        try:
            from archolith_filter.telemetry import get_filter_telemetry_store
            summary = get_filter_telemetry_store().get_summary()
            return {
                "calls_total": summary.total_calls,
                "filtered_calls": summary.filtered_calls,
                "dedupe_calls": summary.dedupe_calls,
                "fallback_calls": summary.fallback_calls,
                "raw_chars": summary.total_raw_chars,
                "filtered_chars": summary.total_filtered_chars,
                "estimated_raw_tokens": summary.estimated_raw_tokens,
                "estimated_filtered_tokens": summary.estimated_filtered_tokens,
                "estimated_saved_tokens": summary.estimated_saved_tokens,
                "average_savings_pct": summary.average_savings_pct,
            }
        except ImportError:
            return {}
        except Exception:
            return {}
