"""File cache operations extracted from chat.py — upserts, invalidations, and write detection.

Concurrency Contract:
- All file cache operations (_upsert_file_cache, _invalidate_file_cache) must be called
  within the session-level extraction lock (acquired in _run_extraction via get_session_lock).
- This ensures atomicity: file reads, outlines, and cache eviction happen as a single unit.
- No per-cache-operation locking is needed; the session lock provides sufficient protection.
"""

from __future__ import annotations

import hashlib
import json

import structlog

from archolith_proxy.config import get_settings
from archolith_proxy.graph.backend import get_backend
from archolith_proxy.shared.text_utils import _build_outline

__all__ = [
    "_upsert_file_cache",
    "_extract_file_writes",
    "_invalidate_written_files",
    "_invalidate_file_cache",
]

logger = structlog.get_logger()


async def _upsert_file_cache(session_id: str, file_reads: list[dict], turn: int) -> None:
    """Store file-read content into the graph backend's file content cache.

    Skips files exceeding the configured max byte size. Uses sha256
    deduplication — if the file's content hasn't changed, no DB write
    is needed (common case on re-reads of unmodified files).

    After all upserts, evicts stale entries based on configured TTL and max entry count.
    """
    settings = get_settings()
    backend = get_backend()
    for fr in file_reads:
        content = fr["content"]
        if len(content.encode()) > settings.file_cache_max_file_bytes:
            logger.debug("file_cache_skipped_too_large", path=fr["path"], session_id=session_id)
            continue
        sha256 = hashlib.sha256(content.encode()).hexdigest()
        try:
            await backend.upsert_file_content(
                session_id=session_id, path=fr["path"],
                content=content, sha256=sha256, turn=turn,
            )
        except Exception:
            logger.warning("file_cache_upsert_failed", path=fr["path"], session_id=session_id, exc_info=True)
            continue

        # Fail-open: a missing or empty outline is non-fatal.
        outline = _build_outline(content, fr["path"])
        if outline:
            try:
                await backend.upsert_file_outline(
                    session_id=session_id, path=fr["path"],
                    outline=outline, turn=turn,
                )
            except Exception:
                logger.debug("file_outline_upsert_failed", path=fr["path"], session_id=session_id)

    # Evict stale entries after all upserts
    if file_reads:
        try:
            await backend.evict_stale_file_cache(
                session_id=session_id,
                max_turns_age=settings.file_cache_ttl_turns,
                max_entries=settings.file_cache_max_entries,
            )
        except Exception:
            logger.debug("file_cache_eviction_failed", session_id=session_id, exc_info=True)


def _extract_file_writes(messages: list[dict]) -> list[dict]:
    """Extract file content from Write/create_file tool call arguments.

    Unlike _extract_file_reads (which reads content from tool *results*),
    this reads content from tool call *arguments* — Write tools carry the new
    file content in their input, not their output ("file written successfully").

    Scoped to the most recent assistant message only: older Write calls have
    already been superseded and their content should not overwrite fresher reads.

    Handles: Write, write, write_file, create_file, create.
    Skips: Edit — requires applying a patch to cached content (done separately).

    Returns list of {path, content, tool_call_id, tool_name}.
    """
    FULL_WRITE_TOOLS = frozenset({"Write", "write", "write_file", "create_file", "create"})
    results = []

    # Only the most recent assistant message — older writes are stale
    last_assistant: dict | None = None
    for msg in messages:
        if msg.get("role") == "assistant":
            last_assistant = msg
    if not last_assistant:
        return results

    for tc in (last_assistant.get("tool_calls") or []):
        try:
            name = tc["function"]["name"]
            if name not in FULL_WRITE_TOOLS:
                continue
            args = json.loads(tc["function"]["arguments"])
            path = (
                args.get("file_path") or args.get("path")
                or args.get("filePath") or args.get("filename")
                or args.get("target_file") or ""
            )
            content = args.get("content") or args.get("file_content") or ""
            if path and content:
                results.append({
                    "path": path,
                    "content": content,
                    "tool_call_id": tc.get("id", ""),
                    "tool_name": name,
                })
        except (KeyError, json.JSONDecodeError):
            continue
    return results


def _invalidate_written_files(messages: list[dict]) -> list[str]:
    """Return paths of files written/edited in this turn's tool calls."""
    WRITE_TOOLS = frozenset({"Write", "Edit", "write", "edit", "write_file", "edit_file", "create_file", "create"})
    paths: list[str] = []
    call_map: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or []):
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (KeyError, json.JSONDecodeError):
                    args = {}
                call_map[tc["id"]] = (tc["function"]["name"], args)
    for name, args in call_map.values():
        if name in WRITE_TOOLS:
            path = (
                args.get("file_path") or args.get("path")
                or args.get("filePath") or args.get("filename")
                or args.get("target_file") or ""
            )
            if path:
                paths.append(path)
    return paths


async def _invalidate_file_cache(
    session_id: str, paths: list[str], turn_number: int,
) -> None:
    """Remove stale cache entries (both content and outline) for files written/edited this turn."""
    from archolith_proxy.metrics import record_metric

    backend = get_backend()
    for path in paths:
        try:
            deleted_content = await backend.delete_file_content(session_id, path)
            deleted_outline = await backend.delete_file_outline(session_id, path)
            if deleted_content or deleted_outline:
                record_metric("file_cache_invalidations", 1)
                logger.info(
                    "file_cache_invalidated",
                    session_id=session_id, path=path, turn=turn_number,
                    deleted_content=deleted_content, deleted_outline=deleted_outline,
                )
        except Exception:
            logger.warning(
                "file_cache_invalidate_failed",
                session_id=session_id, path=path, exc_info=True,
            )
