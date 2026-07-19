"""Per-tool extraction registry.

Provides both a simple function-based registry and a class-based 
ToolExtractorRegistry compatible with extract_facts_per_tool.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Protocol

import structlog

logger = structlog.get_logger()


class ExtractorProtocol(Protocol):
    may_use_llm: bool

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        ...


class ToolExtractorRegistry:
    """Registry that maps tool names to extractor instances/classes."""

    def __init__(self):
        self._extractors: Dict[str, Any] = {}

    def register(self, tool_name: str, extractor: Any) -> None:
        """Register an extractor for a tool name."""
        self._extractors[tool_name.lower()] = extractor
        logger.debug("per_tool_extractor_registered", tool=tool_name)

    def get(self, tool_name: str) -> Any | None:
        """Get the extractor for a tool name."""
        return self._extractors.get(tool_name.lower())

    def list_tools(self) -> list[str]:
        return list(self._extractors.keys())


# Global registry instance
_global_registry: ToolExtractorRegistry | None = None


def get_registry() -> ToolExtractorRegistry:
    """Get or create the global ToolExtractorRegistry."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolExtractorRegistry()
        _auto_register_builtins(_global_registry)
    return _global_registry


def _auto_register_builtins(registry: ToolExtractorRegistry) -> None:
    """Auto-register the built-in extractors."""
    try:
        from .extractors.read import ReadExtractor
        from .extractors.bash import BashExtractor
        from .extractors.grep import GrepExtractor
        from .extractors.write_edit import WriteEditExtractor
        from .extractors.glob import GlobExtractor
        from .extractors.ls import LsExtractor
        from .extractors.fallback import FallbackExtractor

        registry.register("read", ReadExtractor())
        registry.register("bash", BashExtractor())
        registry.register("grep", GrepExtractor())
        registry.register("write", WriteEditExtractor())
        registry.register("edit", WriteEditExtractor())
        registry.register("glob", GlobExtractor())
        registry.register("ls", LsExtractor())
        registry.register("fallback", FallbackExtractor())
    except Exception as e:
        logger.warning("failed_to_auto_register_extractors", error=str(e))


# Simple function-based helpers (for future use or lighter integration)
_TOOL_FUNCTIONS: Dict[str, Callable] = {}


def register_extractor(tool_name: str, func: Callable) -> None:
    _TOOL_FUNCTIONS[tool_name.lower()] = func


def get_extractor(tool_name: str) -> Callable | None:
    return _TOOL_FUNCTIONS.get(tool_name.lower())


__all__ = [
    "ToolExtractorRegistry",
    "get_registry",
    "register_extractor",
    "get_extractor",
]