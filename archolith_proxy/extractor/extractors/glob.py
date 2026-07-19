"""GlobExtractor — no LLM; splits file list from newline output."""

from __future__ import annotations

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor

__all__ = ["GlobExtractor"]

_MAX_DISPLAY_PATHS = 8


class GlobExtractor(ToolExtractor):
    """Handles Glob tool calls — splits on newlines, counts files."""

    tool_names = ("Glob",)

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        pattern = record.args.get("pattern", "")
        # Split on newlines; filter blank/header lines
        lines = [line.strip() for line in record.result.splitlines() if line.strip()]
        # Filter obvious non-path lines (e.g. "Found X files:").
        # The colon check is intentionally absent: Windows absolute paths start with
        # a drive letter and colon (C:\...), so a blanket colon filter would drop them.
        paths = [line for line in lines if not line.startswith("Found")]

        count = len(paths)
        display = paths[:_MAX_DISPLAY_PATHS]
        display_str = ", ".join(display)
        if count > _MAX_DISPLAY_PATHS:
            display_str += f", ... ({count} total)"

        fact_content = f"[Glob] {pattern} → {count} files: {display_str}"

        return PartialExtractionResult(
            source_tool="Glob",
            facts=[{
                "content": fact_content,
                "fact_type": "tool_result",
                "confidence": 1.0,
            }],
            files_touched=[],
            used_llm=False,
        )
