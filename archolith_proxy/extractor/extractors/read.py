"""Read tool extractor with LLM-based structured JSON output."""

from __future__ import annotations

import json
from typing import Any

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.extractors.llm_json import call_llm_for_structured_extraction

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
        content_preview = getattr(record, "result_preview", "") or ""

        settings = get_settings()

        if settings.per_tool_extraction_enabled and len(content_preview) > 200:
            system_prompt = (
                "You are an expert at analyzing source code. "
                "Return ONLY valid JSON with keys: path, symbols (list), outline, key_sections (list)."
            )
            user_prompt = f"File: {path}\nContent preview:\n{content_preview[:1200]}"

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
                                "content": f"Read file: {path}",
                                "fact_type": "file_state",
                                "confidence": 0.85,
                                "structured": structured,
                            }
                        ],
                        "files_touched": [path] if path else [],
                        "used_llm": True,
                        "usage": {},
                    })()
            except Exception:
                pass

        # Fallback
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
