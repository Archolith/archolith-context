"""WriteEditExtractor — no LLM; file content is already in FileContent cache."""

from __future__ import annotations

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor


def _extract_path(args: dict) -> str:
    return (
        args.get("file_path") or args.get("path")
        or args.get("filePath") or args.get("filename")
        or args.get("target_file") or ""
    )


class WriteEditExtractor(ToolExtractor):
    """Handles Write, Edit, and NotebookEdit tool calls.

    File content is already in the FileContent cache.
    Emits a file_state fact and marks the file as modified.
    """

    tool_names = ("Write", "Edit", "NotebookEdit")

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        path = _extract_path(record.args)
        if not path:
            path = "unknown"

        verb = "written" if record.tool_name in ("Write",) else "edited"
        fact_content = f"[{record.tool_name}] {path} {verb} at turn {turn_number}"

        return PartialExtractionResult(
            source_tool=record.tool_name,
            facts=[
                {
                    "content": fact_content,
                    "fact_type": "file_state",
                    "confidence": 1.0,
                }
            ],
            files_touched=[path] if path and path != "unknown" else [],
            used_llm=False,
        )
