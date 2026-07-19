"""Generic fallback extractor for unknown tools."""

from __future__ import annotations

from typing import Any, Dict


def extract_fallback_tool_result(tool_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic extraction for tools without a specialized handler.
    """
    return {
        "raw_preview": str(tool_result)[:300],
        "note": "Extracted with fallback handler",
    }