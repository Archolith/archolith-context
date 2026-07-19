"""Bash tool extractor (class-based)."""

from __future__ import annotations

from typing import Any


class BashExtractor:
    may_use_llm = True

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        """Extract structured info from a Bash tool result."""
        args = getattr(record, "args", {}) or {}
        command = args.get("command", "")

        # For Phase 1 we return a minimal result.
        # A real version would analyze exit_code + output.
        return type("PartialExtractionResult", (), {
            "facts": [{"content": f"Ran command: {command[:80]}", "fact_type": "observation", "confidence": 0.6}],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()