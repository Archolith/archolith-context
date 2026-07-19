"""Bash tool extractor with structured output."""

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
        args = getattr(record, "args", {}) or {}
        command = args.get("command", "")

        # Structured output
        structured = {
            "command": command,
            "exit_code": None,
            "success": None,
            "errors": [],
            "summary": f"Executed: {command[:60]}",
        }

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Ran bash command: {command[:100]}",
                    "fact_type": "observation",
                    "confidence": 0.7,
                    "structured": structured,
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()