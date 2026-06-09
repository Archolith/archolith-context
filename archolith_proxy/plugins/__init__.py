"""Plugin system for archolith-proxy.

Exports the ProxyPlugin protocol and the PluginRegistry singleton.
Plugins implement ProxyPlugin and call get_plugin_registry().register(self)
at import time or in their own init code. main.py calls activate_all()
at startup and deactivate_all() at shutdown.
"""

from __future__ import annotations

from archolith_proxy.plugins.registry import PluginRegistry, ProxyPlugin

__all__ = ["ProxyPlugin", "PluginRegistry", "get_plugin_registry", "reset_plugin_registry"]

# Process-level registry singleton
_registry: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    """Return the process-level PluginRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry


def reset_plugin_registry() -> None:
    """Reset the plugin registry singleton — for tests only."""
    global _registry
    _registry = None
