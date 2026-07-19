"""Memory recall tool extractor."""

from __future__ import annotations

from typing import Any


class MemoryRecallExtractor:
    may_use_llm = False

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        query = args.get("query", "")

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Memory recall query: {query}",
                    "fact_type": "observation",
                    "confidence": 0.5,
                    "structured": {"query": query},
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()