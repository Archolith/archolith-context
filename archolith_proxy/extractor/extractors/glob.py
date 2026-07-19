"""Glob tool extractor."""

from __future__ import annotations

from typing import Any


class GlobExtractor:
    may_use_llm = False

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        pattern = args.get("pattern", "")

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Glob pattern: {pattern}",
                    "fact_type": "observation",
                    "confidence": 0.6,
                    "structured": {"pattern": pattern},
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()