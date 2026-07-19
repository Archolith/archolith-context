"""DefaultExtractor — LLM catch-all using the existing generic extraction logic."""

from __future__ import annotations

import json

import httpx
import structlog

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.prompts import SYSTEM_PROMPT, build_extraction_prompt
from archolith_proxy.config import get_settings

logger = structlog.get_logger()

__all__ = ["DefaultExtractor"]


class DefaultExtractor(ToolExtractor):
    """Catch-all extractor for unknown or uncategorized tools.

    Uses the existing generic extraction prompt — one LLM call per tool result.
    """

    tool_names = ()  # registered via set_default, not by name
    may_use_llm = True
    llm_requested_tokens = 2000

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        settings = get_settings()

        tool_name = record.tool_name
        tool_result = record.result[:4000]
        user_prompt = build_extraction_prompt(
            turn_number=turn_number,
            user_message="",
            assistant_response=f"Used tool {tool_name}",
            tool_results=tool_result,
            session_goal=session_goal,
        )

        payload = {
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }

        try:

            resp = await http_client.post(
                f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.extractor_api_key}",
                    "Content-Type": "application/json",
                },
                content=json.dumps(payload).encode(),
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]

            # Parse JSON response
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                text = "\n".join(lines)

            parsed = json.loads(text)
            usage_raw = data.get("usage", {})
            usage = {
                "prompt_tokens": usage_raw.get("prompt_tokens", 0) or 0,
                "completion_tokens": usage_raw.get("completion_tokens", 0) or 0,
                "llm_calls": 1,
            }
            facts = parsed.get("facts", [])
            # Prefix with tool name
            for f in facts:
                if isinstance(f, dict):
                    f["content"] = f"[{tool_name}] {f.get('content', '')}"

            # Normalize files_touched to bare strings
            files_touched = []
            for f in parsed.get("files_touched", []):
                if isinstance(f, str):
                    files_touched.append(f)
                elif isinstance(f, dict):
                    path = f.get("path") or f.get("file") or ""
                    if path:
                        files_touched.append(path)

            return PartialExtractionResult(
                source_tool=tool_name,
                facts=facts if isinstance(facts, list) and all(isinstance(f, dict) for f in facts) else [],
                files_touched=files_touched,
                used_llm=True,
                usage=usage,
            )
        except Exception as e:
            from archolith_proxy.extractor.budget import LLMBudgetExceeded
            logger.warning("default_extractor_llm_failed", tool=tool_name, error=str(e))
            return PartialExtractionResult(
                source_tool=tool_name,
                facts=[{
                    "content": f"[{tool_name}] {record.result[:300]}",
                    "fact_type": "tool_result",
                    "confidence": 0.4,
                }],
                files_touched=[],
                used_llm=not isinstance(e, LLMBudgetExceeded),
            )
