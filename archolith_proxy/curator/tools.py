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


async def search_facts_semantic(
    session_id: str, query: str = "", limit: int = 10, **kwargs
) -> str:
    """Search active facts by embedding similarity.

    Ranks facts by cosine similarity to the query embedding, returning
    the top matches sorted by relevance. Falls back to substring matching
    when the embedding API is unavailable or no facts have embeddings.

    Returns a bullet list of matching facts, sorted by relevance.
    """
    if not query:
        return "(no query specified)"

    from archolith_proxy.config import get_settings
    settings = get_settings()

    facts = await get_backend().get_active_facts(session_id, limit=200)
    if not facts:
        return "(no facts stored for this session)"

    # --- helpers -----------------------------------------------------------
    def _cosine(a: list, b: list) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)

    def _substring_fallback(reason: str) -> str:
        query_lower = query.lower()
        matches = [
            f.get("content", "")
            for f in facts
            if query_lower in f.get("content", "").lower()
        ]
        if not matches:
            return f"(no matching facts — {reason}, substring fallback also empty)"
        lines = [f"- {m}" for m in matches[:limit]]
        lines.append(f"(substring fallback — {reason})")
        return "\n".join(lines)

    # --- compute query embedding -------------------------------------------
    query_embedding: list[float] | None = None
    if settings.embedding_api_key:
        try:
            import httpx
            from archolith_proxy.extractor.embeddings import compute_embeddings_batch
            async with httpx.AsyncClient(timeout=10.0) as _client:
                results = await compute_embeddings_batch(_client, [query[:8000]])
            query_embedding = results[0] if results else None
        except Exception as exc:
            logger.warning(
                "search_facts_semantic_embed_failed",
                session_id=session_id,
                error=str(exc),
            )

    if query_embedding is None:
        reason = "no embedding API key" if not settings.embedding_api_key else "embedding call failed"
        return _substring_fallback(reason)

    # --- score facts by cosine similarity ----------------------------------
    scored: list[tuple[float, str]] = []
    no_embed_count = 0
    for f in facts:
        fact_emb = f.get("embedding")
        if fact_emb:
            sim = _cosine(query_embedding, fact_emb)
            scored.append((sim, f.get("content", "")))
        else:
            no_embed_count += 1

    if not scored:
        return _substring_fallback("no facts have stored embeddings")

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [(s, c) for s, c in scored[:limit] if s > 0.05]

    if not top:
        return "(no facts above similarity threshold)"

    lines = [f"- {c}" for _, c in top]
    if no_embed_count > 0:
        lines.append(f"({no_embed_count} facts had no stored embedding and were excluded)")
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


async def select_relevant_turns(
    session_id: str, turn_numbers: list | None = None, **kwargs
) -> str:
    """Record the curator's turn retention decision.

    The actual filtering is applied by rewrite_messages() in the proxy pipeline
    using the retained_turn_numbers captured in the loop. This handler just
    returns a confirmation string for the curator's tool result.
    """
    if not turn_numbers:
        return "Recorded: drop all middle turns (retain none)."
    nums = [int(n) for n in turn_numbers]
    return f"Recorded: retaining turns {nums} (relevance order, most relevant first)."


# Tool name → implementation mapping (used by loop.py for dispatch)
async def get_file_outline(session_id: str, path: str = "", **kwargs) -> str:
    """Get the structural outline of a cached file — functions, classes, and methods
    with their line numbers.

    Use this before get_file_lines on any file over 100 lines to find the exact
    line range you need without reading the full content.

    Returns a list of 'line N: def/class/function <name>' entries, or a message
    if the file is not cached or has no indexed symbols.
    """
    if not path:
        return "(no path specified — use list_session_files to see available files)"

    outline = await get_backend().get_file_outline(session_id, path)
    if not outline:
        return f"(no outline available for: {path} — file may not be cached or has no symbols)"

    return outline


async def prefetch_file(
    session_id: str, path: str = "", focus: str = "", **kwargs,
) -> str:
    """Read a file from the local filesystem and cache it for this session.

    Use this to proactively load files the agent will likely need on the next
    turn — e.g. imports of a file already being edited, test files for a module
    under review, or config files referenced in recent decisions.

    When ``focus`` is provided (e.g. "the auth handler function"), the tool
    returns the structural outline first so you know where everything is,
    then returns the focused section matching your description. The full
    file is cached either way — use get_file_lines later for other sections.

    Only works with absolute paths. Respects file_cache_max_file_bytes limit.
    """
    if not path:
        return "(no path specified — use get_touched_files to find relevant paths)"

    import hashlib
    from pathlib import Path

    from archolith_proxy.config import get_settings

    settings = get_settings()
    file_path = Path(path)

    if not file_path.is_absolute():
        # Try to resolve relative paths against cached file roots
        existing = await get_backend().list_cached_files(session_id)
        resolved = False
        if existing:
            for f in existing:
                cached = f.get("path", "")
                if cached and Path(cached).is_absolute():
                    candidate = Path(cached).parent
                    for _ in range(8):  # max 8 levels up
                        attempt = candidate / path
                        if attempt.exists():
                            file_path = attempt
                            resolved = True
                            break
                        candidate = candidate.parent
                    if resolved:
                        break
        if not resolved:
            return f"(cannot resolve relative path: {path} — use absolute paths or ensure related files are already cached)"

    if not file_path.exists():
        return f"(file not found: {file_path})"

    if not file_path.is_file():
        return f"(not a file: {file_path})"

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"(read error: {exc})"

    byte_size = len(content.encode("utf-8"))
    if byte_size > settings.file_cache_max_file_bytes:
        return f"(file too large: {byte_size:,} bytes, limit {settings.file_cache_max_file_bytes:,})"

    # Upsert into file cache
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    backend = get_backend()
    store_path = str(file_path)

    try:
        await backend.upsert_file_content(
            session_id=session_id, path=store_path,
            content=content, sha256=sha256, turn=0,
        )
    except Exception as exc:
        return f"(cache write failed: {exc})"

    # Build and store structural outline
    all_lines = content.split("\n")
    line_count = len(all_lines)
    outline = ""
    try:
        from archolith_proxy.openai.chat import _build_outline
        outline = _build_outline(content, store_path)
        if outline:
            await backend.upsert_file_outline(
                session_id=session_id, path=store_path,
                outline=outline, turn=0,
            )
    except Exception:
        pass  # outline is non-fatal

    logger.info(
        "prefetch_file_cached",
        session_id=session_id,
        path=store_path,
        lines=line_count,
        bytes=byte_size,
        focus=focus or "(none)",
    )

    # --- Build response: outline + focused or preview section ---
    parts = [f"Cached: {store_path} ({line_count} lines, {byte_size:,} bytes)"]

    if outline:
        parts.append(f"\nOutline:\n{outline}")

    if focus and outline:
        # Find the best-matching symbol for the focus description
        focused_range = _find_focused_range(outline, focus, line_count)
        if focused_range:
            start, end = focused_range
            section = all_lines[start - 1 : end]
            numbered = [f"{i}: {line}" for i, line in enumerate(section, start)]
            parts.append(f"\nFocused section (lines {start}-{end}):\n" + "\n".join(numbered))
        else:
            # No match — fall back to first 20 lines
            preview = [f"{i}: {line}" for i, line in enumerate(all_lines[:20], 1)]
            parts.append(f"\n(no outline match for '{focus}' — showing first 20 lines):\n" + "\n".join(preview))
    else:
        # No focus — show first 20 lines as preview
        preview_count = min(20, line_count)
        preview = [f"{i}: {line}" for i, line in enumerate(all_lines[:preview_count], 1)]
        parts.append("\n" + "\n".join(preview))
        if line_count > 20:
            parts.append(f"\n[use get_file_lines for specific sections]")

    return "\n".join(parts)


def _find_focused_range(
    outline: str, focus: str, total_lines: int
) -> tuple[int, int] | None:
    """Find the line range in the outline that best matches the focus query.

    Returns (start_line, end_line) or None if no reasonable match.
    The range extends from the matched symbol to the next symbol (or EOF).
    """
    import re

    # Parse outline entries: "line N: kind name"
    entries: list[tuple[int, str]] = []
    for line in outline.split("\n"):
        m = re.match(r"line (\d+): (.+)", line.strip())
        if m:
            entries.append((int(m.group(1)), m.group(2)))

    if not entries:
        return None

    # Score each entry by keyword overlap with focus
    focus_lower = focus.lower()
    focus_words = set(re.findall(r"\w+", focus_lower))
    best_score = 0
    best_idx = -1

    for i, (_, symbol) in enumerate(entries):
        sym_lower = symbol.lower()
        # Substring match in either direction
        score = 0
        if focus_lower in sym_lower or sym_lower in focus_lower:
            score += 3
        # Word overlap
        sym_words = set(re.findall(r"\w+", sym_lower))
        overlap = focus_words & sym_words
        score += len(overlap)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_score == 0:
        return None

    start_line = entries[best_idx][0]
    # End at the next symbol or EOF, capped at 80 lines
    if best_idx + 1 < len(entries):
        end_line = min(entries[best_idx + 1][0] - 1, start_line + 80)
    else:
        end_line = min(total_lines, start_line + 80)

    return (start_line, end_line)


TOOL_HANDLERS: dict[str, callable] = {
    "list_session_files": list_session_files,
    "get_file": get_file,
    "get_file_outline": get_file_outline,
    "get_file_lines": get_file_lines,
    "search_facts": search_facts,
    "search_facts_semantic": search_facts_semantic,
    "get_session_goal": get_session_goal,
    "get_recent_decisions": get_recent_decisions,
    "get_touched_files": get_touched_files,
    "get_checkpoint": get_checkpoint,
    "get_open_issues": get_open_issues,
    "get_last_verification": get_last_verification,
    "select_relevant_turns": select_relevant_turns,
    "prefetch_file": prefetch_file,
}
