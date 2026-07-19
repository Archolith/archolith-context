"""Grep tool extractor with optional LLM structured output."""

from __future__ import annotations

from typing import Any

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.extractors.llm_json import call_llm_for_structured_extraction


class GrepExtractor:
    may_use_llm = True

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        pattern = args.get("pattern", "")

        settings = get_settings()

        if settings.per_tool_extraction_enabled:
            system_prompt = (
                "You are an expert at analyzing grep results. "
                "Return ONLY valid JSON with keys: pattern, matches (list of {file, line, symbol})."
            )
            user_prompt = f"Pattern: {pattern}\nResults: (tool output would go here)"

            try:
                structured = await call_llm_for_structured_extraction(
                    http_client=http_client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=settings.extractor_model,
                    base_url=settings.extractor_base_url,
                    api_key=settings.extractor_api_key,
                )
                if "error" not in structured:
                    return type("PartialExtractionResult", (), {
                        "facts": [
                            {
                                "content": f"Grep: {pattern}",
                                "fact_type": "observation",
                                "confidence": 0.75,
                                "structured": structured,
                            }
                        ],
                        "files_touched": [],
                        "used_llm": True,
                        "usage": {},
                    })()
            except Exception:
                pass

        # Fallback
        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Grep pattern: {pattern}",
                    "fact_type": "observation",
                    "confidence": 0.6,
                    "structured": {"pattern": pattern},
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()