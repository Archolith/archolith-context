"""LsExtractor — no LLM; counts files and dirs from directory listing."""

from __future__ import annotations

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor

__all__ = ["LsExtractor"]

_MAX_DISPLAY_NAMES = 6


class LsExtractor(ToolExtractor):
    """Handles LS/list_directory tool calls — counts entries."""

    tool_names = ("LS", "ls", "list_directory", "listdir", "ListDirectory")

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        path = record.args.get("path", "") or record.args.get("dir", "") or ""
        lines = [l.strip() for l in record.result.splitlines() if l.strip()]
        # Classify entries: trailing / means directory
        files = [l for l in lines if not l.endswith("/")]
        dirs = [l for l in lines if l.endswith("/")]

        total = len(files) + len(dirs)
        names = lines[:_MAX_DISPLAY_NAMES]
        names_str = ", ".join(names)
        if total > _MAX_DISPLAY_NAMES:
            names_str += f", ..."

        fact_content = f"[ls] {path}: {total} entries ({len(files)} files, {len(dirs)} dirs) — {names_str}"

        return PartialExtractionResult(
            source_tool="ls",
            facts=[{
                "content": fact_content,
                "fact_type": "tool_result",
                "confidence": 1.0,
            }],
            files_touched=[],
            used_llm=False,
        )
