"""Read tool extractor with optional LLM JSON structured output."""

from __future__ import annotations

from typing import Any

from archolith_proxy.config import get_settings


class ReadExtractor:
    may_use_llm = True

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        path = args.get("file_path") or args.get("path", "")

        settings = get_settings()

        # If per-tool LLM extraction is enabled, we could call the LLM here
        # For now we return structured output (LLM call can be added later)
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