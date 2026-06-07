"""ToolExtractorRegistry — routes tool names to their dedicated extractors.

Exact match first, longest-prefix-match fallback (like IP routing).
This prevents ambiguity when two prefix sentinels could both match
the same tool name.
"""

from __future__ import annotations

import structlog

from archolith_proxy.extractor.base import ToolExtractor

logger = structlog.get_logger()

__all__ = [
    "ToolExtractorRegistry",
    "get_registry",
    "register_extractor",
]


class ToolExtractorRegistry:
    """Routes a tool name to the correct ToolExtractor subclass.

    Exact match first, longest-prefix-match fallback (like IP routing).
    This prevents ambiguity when two prefix sentinels could both match
    the same tool name.

    Override semantics: last-registered wins for the same tool name.
    Plugins registered via entry points or ``register_extractor()`` can
    replace built-in extractors by declaring the same ``tool_names``.
    """

    def __init__(self) -> None:
        self._map: dict[str, ToolExtractor] = {}
        self._default: ToolExtractor | None = None

    def register(self, extractor: ToolExtractor) -> None:
        for name in extractor.tool_names:
            self._map[name] = extractor

    def set_default(self, extractor: ToolExtractor) -> None:
        self._default = extractor

    def clear(self) -> None:
        """Clear all registered extractors (for testing and reset)."""
        self._map.clear()
        self._default = None

    def get(self, tool_name: str) -> ToolExtractor:
        # Exact match first
        if tool_name in self._map:
            return self._map[tool_name]
        # Longest-prefix match — prevents ambiguity when multiple sentinels
        # could prefix-match the same tool name (e.g. "mcp__memory__recall"
        # and "mcp__memory__build" both matching "mcp__memory__build_context").
        match: ToolExtractor | None = None
        match_len = 0
        for registered_name, extractor in self._map.items():
            if tool_name.startswith(registered_name) and len(registered_name) > match_len:
                match = extractor
                match_len = len(registered_name)
        if match is not None:
            return match
        if self._default is not None:
            return self._default
        raise LookupError(f"No extractor registered for {tool_name!r} and no default set")

    @classmethod
    def build_default(cls) -> ToolExtractorRegistry:
        from archolith_proxy.extractor.extractors.read import ReadExtractor
        from archolith_proxy.extractor.extractors.write_edit import WriteEditExtractor
        from archolith_proxy.extractor.extractors.bash import BashExtractor
        from archolith_proxy.extractor.extractors.grep import GrepExtractor
        from archolith_proxy.extractor.extractors.glob import GlobExtractor
        from archolith_proxy.extractor.extractors.ls import LsExtractor
        from archolith_proxy.extractor.extractors.find import FindExtractor
        from archolith_proxy.extractor.extractors.web_search import WebSearchExtractor
        from archolith_proxy.extractor.extractors.web_fetch import WebFetchExtractor
        from archolith_proxy.extractor.extractors.memory_recall import MemoryRecallExtractor
        from archolith_proxy.extractor.extractors.default import DefaultExtractor

        reg = cls()
        reg.register(ReadExtractor())
        reg.register(WriteEditExtractor())
        reg.register(BashExtractor())
        reg.register(GrepExtractor())
        reg.register(GlobExtractor())
        reg.register(LsExtractor())
        reg.register(FindExtractor())
        reg.register(WebSearchExtractor())
        reg.register(WebFetchExtractor())
        reg.register(MemoryRecallExtractor())
        reg.set_default(DefaultExtractor())

        # Discover plugins via entry points — last-registered wins for same key,
        # so plugins can override built-ins.
        _discover_extractor_plugins(reg)

        return reg


_REGISTRY: ToolExtractorRegistry | None = None


def _discover_extractor_plugins(reg: ToolExtractorRegistry) -> None:
    """Discover and register extractor plugins declared via entry points.

    Iterates ``archolith.tool_extractors`` entry points. Each entry point
    must resolve to a callable that returns a ``ToolExtractor`` instance.
    Plugins that fail to load are logged and skipped — they never prevent
    proxy startup.
    """
    from importlib.metadata import entry_points

    for ep in entry_points(group="archolith.tool_extractors"):
        try:
            extractor = ep.load()()
            reg.register(extractor)
            logger.info("extractor_plugin_loaded", entry_point=ep.name, tool_names=extractor.tool_names)
        except Exception:
            logger.exception("extractor_plugin_load_failed", entry_point=ep.name)


def get_registry() -> ToolExtractorRegistry:
    """Return the process-level singleton registry (lazy-built)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ToolExtractorRegistry.build_default()
    return _REGISTRY


def register_extractor(extractor: ToolExtractor) -> None:
    """Programmatically register a ToolExtractor into the process-level singleton.

    This is an alternative to entry points — useful for testing and embedded use.
    Last-registered wins: if the extractor's ``tool_names`` overlap an existing
    registration, the new extractor replaces it.
    """
    get_registry().register(extractor)
