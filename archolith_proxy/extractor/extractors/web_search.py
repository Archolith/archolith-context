"""Web search tool extractor with LLM-powered structured JSON output."""

from __future__ import annotations

from typing import Any

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.extractors.llm_json import call_llm_for_structured_extraction


class WebSearchExtractor:
    may_use_llm = True

    async def extract(
        self,
        record: Any,
        http_client: Any,
        turn_number: int,
        session_goal: str | None = None,
    ) -> Any:
        args = getattr(record, "args", {}) or {}
        query = args.get("query", "")

        settings = get_settings()

        if settings.per_tool_extraction_enabled:
            system_prompt = (
                "You are an expert at analyzing web search queries. "
                "Return ONLY valid JSON with keys: query, summary."
            )
            user_prompt = f"Web search query: {query}"

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
                                "content": f"Web search: {query}",
                                "fact_type": "observation",
                                "confidence": 0.6,
                                "structured": structured,
                            }
                        ],
                        "files_touched": [],
                        "used_llm": True,
                        "usage": {},
                    })()
            except Exception:
                pass

        return type("PartialExtractionResult", (), {
            "facts": [
                {
                    "content": f"Web search: {query}",
                    "fact_type": "observation",
                    "confidence": 0.5,
                    "structured": {"query": query},
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()