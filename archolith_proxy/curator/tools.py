"""Curator tool implementations — 10 async tool functions.

Each is `async def tool_name(session_id: str, **kwargs) -> str`.
All call `get_backend()` directly. No LLM calls — pure DB queries
returning formatted strings.
"""

from __future__ import annotations

import structlog

from archolith_proxy.graph.backend import get_backend

logger = structlog.get_logger()

# Threshold for truncating full-file content (use get_file_lines instead)
_FULL_FILE_LINE_LIMIT = 200
_FULL_FILE_PREVIEW_LINES = 10


async def list_session_files(session_id: str, **kwargs) -> str:
    """List all cached files for the session.

    Returns a markdown table: path, lines, last-turn.
    """
    files = await get_backend().list_cached_files(session_id)
    if not files:
        return "(no cached files for this session)"

    lines = ["| Path | Lines | Last Turn |", "|------|-------|-----------|"]
    for f in files:
        lines.append(f"| {f.get('path', '?')} | {f.get('line_count', 0)} | {f.get('last_updated_turn', 0)} |")
    return "\n".join(lines)


async def get_file(session_id: str, path: str = "", **kwargs) -> str:
    """Get cached file content. Returns full content for small files,
    or a truncation hint for large files (>200 lines)."""
    if not path:
        return "(no path specified — use list_session_files to see available files)"

    result = await get_backend().get_file_content(session_id, path)
    if not result:
        return f"(file not cached: {path})"

    content = result.get("content", "")
    line_count = result.get("line_count", 0)

    if line_count <= _FULL_FILE_LINE_LIMIT:
        # Prepend line numbers
        numbered = []
        for i, line in enumerate(content.split("\n"), 1):
            numbered.append(f"{i}: {line}")
        return "\n".join(numbered)

    # Large file: return first 10 lines + hint
    preview_lines = content.split("\n")[:_FULL_FILE_PREVIEW_LINES]
    numbered = []
    for i, line in enumerate(preview_lines, 1):
        numbered.append(f"{i}: {line}")
    preview = "\n".join(numbered)
    return f"{preview}\n\n[file has {line_count} lines — use get_file_lines(start_line, end_line) to retrieve specific sections]"


async def get_file_lines(
    session_id: str, path: str = "", start_line: int = 1, end_line: int = 50, **kwargs,
) -> str:
    """Retrieve specific line range from cached file content.

    Prefer this over get_file for large files. Line numbers are 1-indexed
    and inclusive. Out-of-range end is clamped to EOF.
    """
    if not path:
        return "(no path specified)"

    result = await get_backend().get_file_lines(session_id, path, start_line, end_line)
    if not result:
        return f"(file not cached or no lines in range: {path} {start_line}-{end_line})"
    return result


async def search_facts(session_id: str, query: str = "", **kwargs) -> str:
    """Search active facts by keyword substring match.

    Returns a bullet list of matching facts, or '(no matching facts)'.
    """
    if not query:
        return "(no query specified)"

    facts = await get_backend().get_active_facts(session_id, limit=100)
    if not facts:
        return "(no matching facts)"

    query_lower = query.lower()
    matches = []
    for f in facts:
        content = f.get("content", "")
        if query_lower in content.lower():
            matches.append(content)

    if not matches:
        return "(no matching facts)"

    lines = []
    for m in matches[:20]:  # Cap at 20 results
        lines.append(f"- {m}")
    return "\n".join(lines)


async def get_session_goal(session_id: str, **kwargs) -> str:
    """Get the session goal string."""
    session = await get_backend().find_session_by_id(session_id)
    if not session:
        return "(session not found)"
    goal = session.get("goal", "")
    if not goal:
        return "(no session goal set)"
    return goal


async def get_recent_decisions(session_id: str, **kwargs) -> str:
    """Get recent decisions for the session as a numbered list."""
    decisions = await get_backend().get_decisions(session_id)
    if not decisions:
        return "(no decisions recorded)"

    lines = []
    for i, d in enumerate(decisions, 1):
        summary = d.get("summary", "")
        turn = d.get("turn", "?")
        lines.append(f"{i}. [turn {turn}] {summary}")
    return "\n".join(lines)


async def get_touched_files(session_id: str, **kwargs) -> str:
    """Get all files touched in the session as a table: path, status, turn."""
    files = await get_backend().get_touched_files(session_id)
    if not files:
        return "(no touched files)"

    lines = ["| Path | Status | Turn |", "|------|--------|------|"]
    for f in files:
        path = f.get("path", "?")
        status = f.get("status", "?")
        turn = f.get("last_modified_turn") or f.get("last_read_turn", 0)
        lines.append(f"| {path} | {status} | {turn} |")
    return "\n".join(lines)

async def get_checkpoint(session_id: str, **kwargs) -> str:
    """Get the current work checkpoint: what state the session is in and what comes next."""
    checkpoint = await get_backend().get_checkpoint(session_id)
    if not checkpoint:
        return "(no checkpoint recorded yet — not enough turns)"
    summary = checkpoint.get("summary", "")
    next_step = checkpoint.get("next_step", "")
    confidence = checkpoint.get("confidence", 0.5)
    turn = checkpoint.get("source_turn", 0)
    lines = [f"**Current state** (turn {turn}, confidence {confidence:.0%}): {summary}"]
    if next_step:
        lines.append(f"**Next step**: {next_step}")
    return "\n".join(lines)


async def get_open_issues(session_id: str, **kwargs) -> str:
    """Get all open (unresolved) issues: errors, blockers, and failing tests."""
    issues = await get_backend().get_open_issues(session_id)
    if not issues:
        return "(no open issues)"
    lines = []
    for i, issue in enumerate(issues, 1):
        summary = issue.get("summary", "")
        related_file = issue.get("related_file", "")
        related_command = issue.get("related_command", "")
        turn = issue.get("source_turn", 0)
        line = f"{i}. [turn {turn}] {summary}"
        if related_file:
            line += f" (file: {related_file})"
        if related_command:
            line += f" (cmd: {related_command})"
        lines.append(line)
    return "\n".join(lines)


async def get_last_verification(session_id: str, **kwargs) -> str:
    """Get the most recent test/verification result: command, pass/fail, and what was tested."""
    v = await get_backend().get_last_verification(session_id)
    if not v:
        return "(no verifications recorded)"
    command = v.get("command", "")
    status = v.get("status", "")
    summary = v.get("summary", "")
    turn = v.get("source_turn", 0)
    icon = {"pass": "PASS", "fail": "FAIL", "partial": "PARTIAL"}.get(status, status.upper())
    return f"[{icon}] turn {turn}: `{command}`\n{summary}"


# Tool name → implementation mapping (used by loop.py for dispatch)
TOOL_HANDLERS: dict[str, callable] = {
    "list_session_files": list_session_files,
    "get_file": get_file,
    "get_file_lines": get_file_lines,
    "search_facts": search_facts,
    "get_session_goal": get_session_goal,
    "get_recent_decisions": get_recent_decisions,
    "get_touched_files": get_touched_files,
    "get_checkpoint": get_checkpoint,
    "get_open_issues": get_open_issues,
    "get_last_verification": get_last_verification,
}
