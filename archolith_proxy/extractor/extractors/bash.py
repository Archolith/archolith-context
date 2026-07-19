"""Bash tool extractor with LLM-based structured JSON output."""

from __future__ import annotations

import json
from typing import Any

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.extractors.llm_json import call_llm_for_structured_extraction


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
        output = getattr(record, "result_preview", "") or ""

        settings = get_settings()

        # Use LLM for structured analysis when enabled
        if settings.per_tool_extraction_enabled:
            system_prompt = (
                "You are an expert at analyzing bash command output. "
                "Return ONLY valid JSON with the following keys: "
                "command, exit_code, success (bool), errors (list), summary."
            )
            user_prompt = f"Command: {command}\nOutput: {output[:800]}"

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
                                "content": f"Ran bash command: {command[:100]}",
                                "fact_type": "observation",
                                "confidence": 0.8,
                                "structured": structured,
                                "used_llm": True,
                            }
                        ],
                        "files_touched": [],
                        "used_llm": True,
                        "usage": {},
                    })()
            except Exception:
                pass  # Fall back to non-LLM path

        # Fallback structured output
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
                    "used_llm": False,
                }
            ],
            "files_touched": [],
            "used_llm": False,
            "usage": {},
        })()
