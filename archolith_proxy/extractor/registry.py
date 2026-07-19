"""Per-tool extraction registry.

Maps tool names to their specialized extractor functions.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

logger = __import__("structlog").get_logger()

# Tool name → extractor function mapping
_TOOL_EXTRACTORS: Dict[str, Callable] = {}


def register_extractor(tool_name: str, func: Callable) -> None:
    """Register a specialized extractor for a tool."""
    _TOOL_EXTRACTORS[tool_name.lower()] = func
    logger.debug("per_tool_extractor_registered", tool=tool_name)


def get_extractor(tool_name: str) -> Callable | None:
    """Get the extractor for a tool, or None if not registered."""
    return _TOOL_EXTRACTORS.get(tool_name.lower())


def list_registered_tools() -> list[str]:
    """Return list of registered tool names."""
    return list(_TOOL_EXTRACTORS.keys())


# Auto-register built-in extractors
try:
    from .read import extract_read_tool_result
    from .bash import extract_bash_tool_result
    from .fallback import extract_fallback_tool_result

    register_extractor("read", extract_read_tool_result)
    register_extractor("bash", extract_bash_tool_result)
    register_extractor("fallback", extract_fallback_tool_result)
except ImportError:
    pass


__all__ = [
    "register_extractor",
    "get_extractor",
    "list_registered_tools",
]