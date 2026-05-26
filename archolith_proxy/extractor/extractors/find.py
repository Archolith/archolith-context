"""FindExtractor — no LLM; counts found paths."""

from __future__ import annotations

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor

_MAX_DISPLAY_PATHS = 8


class FindExtractor(ToolExtractor):
    """Handles find/FindFiles tool calls — counts matching paths."""

    tool_names = ("find", "FindFiles", "find_files")

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        lines = [l.strip() for l in record.result.splitlines() if l.strip()]
        paths = [l for l in lines if not l.startswith("Found") and len(l) > 1]

        count = len(paths)
        display = paths[:_MAX_DISPLAY_PATHS]
        display_str = ", ".join(display)
        if count > _MAX_DISPLAY_PATHS:
            display_str += f", ... ({count} total)"

        fact_content = f"[find] found {count} paths matching query: {display_str}"

        return PartialExtractionResult(
            source_tool="find",
            facts=[{
                "content": fact_content,
                "fact_type": "tool_result",
                "confidence": 1.0,
            }],
            files_touched=[],
            used_llm=False,
        )
