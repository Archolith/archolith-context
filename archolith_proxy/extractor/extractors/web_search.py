"""WebSearchExtractor — no LLM; parses structured JSON or text search results."""

from __future__ import annotations

import json
import re

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor

__all__ = ["WebSearchExtractor"]

_MAX_RESULTS = 5

_TITLE_RE = re.compile(r"(?:Title|title):\s*(.+)")
_URL_RE = re.compile(r"(?:URL|url|Link|link):\s*(\S+)")
_SNIPPET_RE = re.compile(r"(?:Snippet|snippet|Description|description):\s*(.+)")


class WebSearchExtractor(ToolExtractor):
    """Handles WebSearch tool calls — parses structured results without LLM.

    Parse order: (1) JSON array of result objects with title/url/snippet keys,
    (2) line-regex fallback for Title:/URL:/Snippet: plain-text formats.
    """

    tool_names = ("WebSearch", "web_search", "websearch")

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        query = record.args.get("query", "") or record.args.get("term", "") or ""
        results = self._parse_json(record.result)
        if not results:
            results = self._parse_regex(record.result)

        if not results:
            # Raw fallback
            return PartialExtractionResult(
                source_tool="web_search",
                facts=[{
                    "content": f"[web_search] '{query}' — {record.result[:300]}",
                    "fact_type": "tool_result",
                    "confidence": 0.5,
                }],
                files_touched=[],
                used_llm=False,
            )

        facts = []
        for item in results[:_MAX_RESULTS]:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("snippet", "")[:120]
            facts.append({
                "content": f"[web_search] '{query}': '{title}' — {snippet} ({url})",
                "fact_type": "tool_result",
                "confidence": 0.8,
            })

        return PartialExtractionResult(
            source_tool="web_search",
            facts=facts,
            files_touched=[],
            used_llm=False,
        )

    @staticmethod
    def _parse_json(text: str) -> list[dict]:
        """Try to parse structured JSON results."""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            # Could be wrapped: {"results": [...]} or {"web": {"results": [...]}}
            items = (
                data.get("results")
                or data.get("web", {}).get("results")
                or data.get("organic_results")
                or []
            )
        else:
            return []
        if not isinstance(items, list):
            return []
        # Validate each item has at least one recognizable key
        valid = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if any(k in item for k in ("title", "url", "snippet", "description", "link")):
                # Normalize key names
                entry = {
                    "title": item.get("title", ""),
                    "url": item.get("url") or item.get("link", ""),
                    "snippet": item.get("snippet") or item.get("description", ""),
                }
                valid.append(entry)
        return valid

    @staticmethod
    def _parse_regex(text: str) -> list[dict]:
        """Fallback: parse Title:/URL:/Snippet: line patterns."""
        titles = _TITLE_RE.findall(text)
        urls = _URL_RE.findall(text)
        snippets = _SNIPPET_RE.findall(text)
        if not titles:
            return []
        results = []
        for i, title in enumerate(titles):
            results.append({
                "title": title.strip(),
                "url": urls[i].strip() if i < len(urls) else "",
                "snippet": snippets[i].strip() if i < len(snippets) else "",
            })
        return results
