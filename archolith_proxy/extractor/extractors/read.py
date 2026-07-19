"""Read tool extractor (class-based)."""

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
        """Extract structured info from a Read tool result."""
        args = getattr(record, "args", {}) or {}
        path = args.get("file_path") or args.get("path", "")

        # In a real implementation we would use the file content from the tool result.
        # For Phase 1 we return a minimal structured result.
        return type("PartialExtractionResult", (), {
            "facts": [{"content": f"Read file: {path}", "fact_type": "file_state", "confidence": 0.7}],
            "files_touched": [path] if path else [],
            "used_llm": False,
            "usage": {},
        })()