"""Ls tool extractor."""

from __future__ import annotations

from typing import Any


class LsExtractor:
    may_use_llm = False

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        path = args.get("path", ".")

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Listed directory: {path}",
                    "fact_type": "observation",
                    "confidence": 0.5,
                    "structured": {"path": path},
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()