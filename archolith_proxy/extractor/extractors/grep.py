"""GrepExtractor — no LLM; parses path:line:match triples directly."""

from __future__ import annotations

import re

import httpx

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor

__all__ = ["GrepExtractor"]

# Lazy path match before :line_number: — handles both Unix (/path/file.py:42:match)
# and Windows (C:\path\file.py:42:match) paths. The lazy .+? naturally finds the
# first :digits: boundary, correctly treating the Windows drive colon as path content.
_GREP_LINE_RE = re.compile(r"^(.+?):(\d+):(.+)$", re.MULTILINE)
_MAX_LINES_PER_FILE = 5
_MAX_FILES = 10


def _is_valid_path(s: str) -> bool:
    """Check if a string looks like a real file path (not just a label:line:text pattern).

    Heuristics:
    - Contains at least one path separator (/, \) or starts with drive letter (C:)
    - Doesn't look like a simple label (e.g. "label:line:content")
    - Has reasonable path characters
    """
    if not s:
        return False
    # Path separators are good sign
    if "/" in s or "\\" in s:
        return True
    # Windows drive letter (e.g. C:\...)
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        return True
    # Likely just a label if it has no path separators
    return False


class GrepExtractor(ToolExtractor):
    """Handles Grep tool calls — parses structured output without LLM."""

    tool_names = ("Grep",)

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        pattern = record.args.get("pattern", "")
        matches = _GREP_LINE_RE.findall(record.result)

        if not matches:
            # Fallback: no path:line:structure → one generic fact
            return PartialExtractionResult(
                source_tool="Grep",
                facts=[{
                    "content": f"[Grep] '{pattern}' — no structured matches; raw output: {record.result[:300]}",
                    "fact_type": "tool_result",
                    "confidence": 0.6,
                }],
                files_touched=[],
                used_llm=False,
            )

        # Group by file path (filter out non-path strings)
        by_file: dict[str, list[int]] = {}
        for path, line, _content in matches:
            # Validate that the path looks like a real file path
            if _is_valid_path(path):
                by_file.setdefault(path, []).append(int(line))

        facts = []
        files_touched = []
        for i, (path, lines) in enumerate(by_file.items()):
            if i >= _MAX_FILES:
                break
            # Cap displayed line numbers
            display_lines = lines[:_MAX_LINES_PER_FILE]
            lines_str = ", ".join(str(n) for n in display_lines)
            if len(lines) > _MAX_LINES_PER_FILE:
                lines_str += f", ... ({len(lines)} total)"
            facts.append({
                "content": f"[Grep] '{pattern}' matched {len(lines)} times in {path} (lines {lines_str})",
                "fact_type": "tool_result",
                "confidence": 1.0,
            })
            files_touched.append(path)

        return PartialExtractionResult(
            source_tool="Grep",
            facts=facts,
            files_touched=files_touched,
            used_llm=False,
        )
