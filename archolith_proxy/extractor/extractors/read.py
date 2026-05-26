"""ReadExtractor — no LLM; file content is already in FileContent cache."""

from __future__ import annotations

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor


def _extract_path(args: dict) -> str:
    return (
        args.get("file_path") or args.get("path")
        or args.get("filePath") or args.get("filename")
        or args.get("target_file") or ""
    )


class ReadExtractor(ToolExtractor):
    """Handles Read tool calls.

    File content is already in the FileContent cache via _upsert_file_cache().
    A second extraction pass is redundant — just emit a provenance fact.
    """

    tool_names = ("Read",)

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        path = _extract_path(record.args)
        if not path:
            # Fall back: try to infer from result content (first line often has path)
            first_line = record.result.splitlines()[0] if record.result else ""
            path = first_line.strip() or "unknown"

        # Count lines from result for a richer fact
        line_count = record.result.count("\n") + 1 if record.result.strip() else 0
        line_note = f" ({line_count} lines)" if line_count > 0 else ""

        fact_content = f"[Read] {path} read at turn {turn_number}{line_note}"

        return PartialExtractionResult(
            source_tool="Read",
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
