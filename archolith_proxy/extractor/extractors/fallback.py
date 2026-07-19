"""Fallback extractor for unknown tools."""

from __future__ import annotations

from typing import Any


class FallbackExtractor:
    may_use_llm = False

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        tool_name = getattr(record, "tool_name", "unknown")
        return type("PartialExtractionResult", (), {
            "facts": [{"content": f"Tool {tool_name} executed", "fact_type": "observation", "confidence": 0.4}],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()