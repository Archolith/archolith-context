"""MemoryRecallExtractor — no LLM; recalled memories are already structured facts."""

from __future__ import annotations

import json

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor

_MAX_ITEMS = 20
_MIN_SCORE = 0.5
_DEFAULT_CONFIDENCE = 0.75


class MemoryRecallExtractor(ToolExtractor):
    """Handles mcp__memory__recall* tool calls.

    Recalled memories are pre-structured facts — no LLM needed.
    Prefix sentinel "mcp__memory__recall" covers recall_memories,
    recall_context_memories, and any future recall_* variants via
    the registry's longest-prefix-match.
    """

    tool_names = ("mcp__memory__recall",)

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        items = self._parse_json(record.result)
        if not items:
            items = self._parse_text(record.result)

        facts = []
        for item in items[:_MAX_ITEMS]:
            text = item.get("text", "")
            score = item.get("score", _DEFAULT_CONFIDENCE)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = _DEFAULT_CONFIDENCE
            # Filter low scores BEFORE clamping — a score of 0.3 should be excluded
            if score < _MIN_SCORE:
                continue
            # Clamp to valid range
            score = max(0.5, min(1.0, score))
            facts.append({
                "content": f"[memory_recall] {text}",
                "fact_type": "observation",
                "confidence": score,
            })

        return PartialExtractionResult(
            source_tool="memory_recall",
            facts=facts,
            files_touched=[],
            used_llm=False,
        )

    @staticmethod
    def _parse_json(text: str) -> list[dict]:
        """Try to parse as JSON (list of items or structured response)."""
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, list):
            return data if all(isinstance(item, dict) for item in data) else []
        if isinstance(data, dict):
            # Common memory response shapes
            items = data.get("memories") or data.get("results") or data.get("items") or []
            if isinstance(items, list):
                return items if all(isinstance(item, dict) for item in items) else []
        return []

    @staticmethod
    def _parse_text(text: str) -> list[dict]:
        """Fallback: split on --- or blank-line separators."""
        segments = text.split("---")
        if len(segments) <= 1:
            segments = text.split("\n\n")
        items = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            items.append({"text": seg, "score": _DEFAULT_CONFIDENCE})
        return items
