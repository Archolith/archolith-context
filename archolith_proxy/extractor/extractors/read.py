"""Read tool extractor with structured output."""

from __future__ import annotations

from typing import Any


class ReadExtractor:
    may_use_llm = False

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        path = args.get("file_path") or args.get("path", "")

        # Structured output
        structured = {
            "path": path,
            "symbols": [],
            "outline": f"File {path}",
            "key_sections": [],
        }

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Read file: {path}",
                    "fact_type": "file_state",
                    "confidence": 0.8,
                    "structured": structured,
                }
            ],
            "files_touched": [path] if path else [],
            "used_llm": False,
            "usage": {},
        })()