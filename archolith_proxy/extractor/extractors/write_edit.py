"""Write/Edit tool extractor."""

from __future__ import annotations

from typing import Any


class WriteEditExtractor:
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

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Modified file: {path}",
                    "fact_type": "file_state",
                    "confidence": 0.75,
                    "structured": {"path": path, "action": "write_or_edit"},
                }
            ],
            "files_touched": [path] if path else [],
            "used_llm": False,
            "usage": {},
        })()