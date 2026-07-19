"""Specialized extractor for Read tool results."""

from __future__ import annotations

from typing import Any, Dict


def extract_read_tool_result(tool_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract structured information from a Read tool result.

    Expected input:
        {
            "path": "...",
            "content": "...",
            "lines_read": ...
        }
    """
    path = tool_result.get("path", "")
    content = tool_result.get("content", "")

    # Very simple symbol extraction (can be improved later with AST)
    symbols = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("class ") or stripped.startswith("def "):
            symbol = stripped.split("(")[0].split(":")[0].strip()
            symbols.append(symbol)

    return {
        "path": path,
        "symbols": symbols[:20],  # Limit to avoid huge facts
        "outline": f"File {path} with {len(symbols)} symbols",
        "key_sections": [],
    }