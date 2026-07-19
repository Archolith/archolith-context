"""Write/Edit tool extractor with LLM-powered structured JSON output."""

from __future__ import annotations

from typing import Any

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.extractors.llm_json import call_llm_for_structured_extraction


class WriteEditExtractor:
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

        if settings.per_tool_extraction_enabled:
            system_prompt = (
                "You are an expert at analyzing file write/edit operations. "
                "Return ONLY valid JSON with keys: path, action, summary."
            )
            user_prompt = f"File: {path}\nAction: write or edit"

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
                                "content": f"Modified file: {path}",
                                "fact_type": "file_state",
                                "confidence": 0.8,
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