"""ToolExtractorRegistry — routes tool names to their dedicated extractors.

Exact match first, longest-prefix-match fallback (like IP routing).
This prevents ambiguity when two prefix sentinels could both match
the same tool name.
"""

from __future__ import annotations

import structlog

from archolith_proxy.extractor.base import ToolExtractor

logger = structlog.get_logger()


class ToolExtractorRegistry:
    """Routes a tool name to the correct ToolExtractor subclass."""

    def __init__(self) -> None:
        self._map: dict[str, ToolExtractor] = {}
        self._default: ToolExtractor | None = None

    def register(self, extractor: ToolExtractor) -> None:
        for name in extractor.tool_names:
            self._map[name] = extractor

    def set_default(self, extractor: ToolExtractor) -> None:
        self._default = extractor

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
        return reg


_REGISTRY: ToolExtractorRegistry | None = None


def get_registry() -> ToolExtractorRegistry:
    """Return the process-level singleton registry (lazy-built)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ToolExtractorRegistry.build_default()
    return _REGISTRY
