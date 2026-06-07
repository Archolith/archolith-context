"""WebFetchExtractor — LLM call with WEB_FETCH_SYSTEM_PROMPT."""

from __future__ import annotations

import json
import re

import httpx
import structlog

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.prompts import WEB_FETCH_SYSTEM_PROMPT, build_web_fetch_extraction_prompt
from archolith_proxy.config import get_settings

logger = structlog.get_logger()

__all__ = ["WebFetchExtractor"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class WebFetchExtractor(ToolExtractor):
    """Handles WebFetch tool calls — LLM extracts technical observations."""

    tool_names = ("WebFetch", "web_fetch", "webfetch", "fetch")
    may_use_llm = True  # always makes one LLM call

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        url = record.args.get("url", "")
        content = _ANSI_RE.sub("", record.result)[:4000]

        settings = get_settings()
        user_prompt = build_web_fetch_extraction_prompt(url, content, turn_number)

        payload = {
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": WEB_FETCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1000,
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
            parsed = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())

            facts = parsed.get("facts", [])
            # Prefix with [web_fetch]
            prefixed = []
            for f in facts:
                if isinstance(f, dict):
                    f["content"] = f"[web_fetch] {f.get('content', '')}"
                    f.setdefault("fact_type", "observation")
                    prefixed.append(f)
                elif isinstance(f, str):
                    prefixed.append({
                        "content": f"[web_fetch] {f}",
                        "fact_type": "observation",
                        "confidence": 0.7,
                    })

            return PartialExtractionResult(
                source_tool="web_fetch",
                facts=prefixed,
                files_touched=[],
                used_llm=True,
            )
        except Exception as e:
            logger.warning("web_fetch_extractor_llm_failed", error=str(e))
            return PartialExtractionResult(
                source_tool="web_fetch",
                facts=[{
                    "content": f"[web_fetch] {url}: {content[:200]}",
                    "fact_type": "observation",
                    "confidence": 0.4,
                }],
                files_touched=[],
                used_llm=True,
            )
