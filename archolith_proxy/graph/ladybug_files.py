"""LadybugDB file content cache and outline operations."""

from __future__ import annotations

from uuid import uuid4

import structlog

logger = structlog.get_logger()


async def upsert_file_content(execute, session_id: str, path: str, content: str, sha256: str, turn: int) -> None:
    existing = await execute(
        "MATCH (fc:FileContent {session_id: $session_id, path: $path}) RETURN fc.sha256 AS sha256, fc.file_id AS file_id",
        {"session_id": session_id, "path": path},
    )
    if existing:
        existing_sha = existing[0].get("sha256")
        if existing_sha == sha256:
            logger.debug("file_cache_hit", path=path, session_id=session_id)
            return
        fid = existing[0].get("file_id")
        line_count = content.count("\n") + 1
        await execute(
            """
            MATCH (fc:FileContent {file_id: $fid})
            SET fc.content = $content, fc.sha256 = $sha256,
                fc.line_count = $line_count, fc.last_updated_turn = $turn
            """,
            {"fid": fid, "content": content, "sha256": sha256,
             "line_count": line_count, "turn": turn},
        )
        logger.debug("file_cache_updated", path=path, session_id=session_id)
    else:
        fid = "fc" + uuid4().hex[:14]
        line_count = content.count("\n") + 1
        await execute(
            """
            CREATE (fc:FileContent {
                file_id: $fid, session_id: $session_id, path: $path,
                content: $content, sha256: $sha256, line_count: $line_count,
                last_updated_turn: $turn, created_at: current_timestamp()
            })
            """,
            {"fid": fid, "session_id": session_id, "path": path,
             "content": content, "sha256": sha256,
             "line_count": line_count, "turn": turn},
        )
        logger.debug("file_cache_created", path=path, session_id=session_id)


async def get_file_content(execute, session_id: str, path: str) -> dict | None:
    rows = await execute(
        "MATCH (fc:FileContent {session_id: $session_id, path: $path}) RETURN fc.content AS content, fc.sha256 AS sha256, fc.line_count AS line_count",
        {"session_id": session_id, "path": path},
    )
    if rows:
        return rows[0]

    norm_query = path.replace("\\", "/").lstrip("/")
    if not norm_query:
        return None
    suffix = "/" + norm_query

    # Single Cypher query instead of N+1 list_cached_files + iteration
    rows = await execute(
        """
        MATCH (fc:FileContent {session_id: $session_id})
        WHERE fc.path = $norm_query OR fc.path ENDS WITH $suffix
        RETURN fc.content AS content, fc.sha256 AS sha256, fc.line_count AS line_count,
               fc.path AS stored_path, length(fc.path) AS path_len
        ORDER BY path_len ASC
        LIMIT 2
        """,
        {"session_id": session_id, "norm_query": norm_query, "suffix": suffix},
    )
    if not rows:
        return None
    if len(rows) > 1 and rows[0]["path_len"] == rows[1]["path_len"]:
        logger.warning(
            "file_recall_ambiguous",
            session_id=session_id, query=path,
            matches=[r["stored_path"] for r in rows],
        )
        return None
    return rows[0]


async def get_file_lines(execute, session_id: str, path: str, start: int, end: int) -> str | None:
    row = await get_file_content(execute, session_id, path)
    if not row:
        return None
    lines = row["content"].split("\n")
    start = max(1, start)
    end = min(end, len(lines))
    if start > end:
        return None
    selected = lines[start - 1:end]
    numbered = [f"{start + i}: {line}" for i, line in enumerate(selected)]
    return "\n".join(numbered)


async def list_cached_files(execute, session_id: str) -> list[dict]:
    return await execute(
        """
        MATCH (fc:FileContent {session_id: $session_id})
        RETURN fc.path AS path, fc.sha256 AS sha256,
               fc.line_count AS line_count, fc.last_updated_turn AS last_updated_turn
        ORDER BY fc.path ASC
        """,
        {"session_id": session_id},
    )


async def delete_file_content(execute, session_id: str, path: str) -> bool:
    rows = await execute(
        "MATCH (fc:FileContent {session_id: $session_id, path: $path}) DELETE fc RETURN count(fc) AS deleted",
        {"session_id": session_id, "path": path},
    )
    deleted = bool(rows and rows[0].get("deleted"))
    if deleted:
        logger.debug("file_cache_deleted", path=path, session_id=session_id)
    return deleted


async def delete_file_outline(execute, session_id: str, path: str) -> bool:
    rows = await execute(
        "MATCH (fo:FileOutline {session_id: $session_id, path: $path}) DELETE fo RETURN count(fo) AS deleted",
        {"session_id": session_id, "path": path},
    )
    deleted = bool(rows and rows[0].get("deleted"))
    if deleted:
        logger.debug("file_outline_deleted", path=path, session_id=session_id)
    return deleted


async def upsert_file_outline(execute, session_id: str, path: str, outline: str, turn: int) -> None:
    existing = await execute(
        "MATCH (fo:FileOutline {session_id: $session_id, path: $path}) RETURN fo.outline_id AS oid",
        {"session_id": session_id, "path": path},
    )
    if existing:
        oid = existing[0].get("oid")
        await execute(
            """
            MATCH (fo:FileOutline {outline_id: $oid})
            SET fo.outline = $outline, fo.last_updated_turn = $turn
            """,
            {"oid": oid, "outline": outline, "turn": turn},
        )
    else:
        oid = "fo" + uuid4().hex[:14]
        await execute(
            """
            CREATE (fo:FileOutline {
                outline_id: $oid, session_id: $sid, path: $path,
                outline: $outline, last_updated_turn: $turn, created_at: current_timestamp()
            })
            """,
            {"oid": oid, "sid": session_id, "path": path, "outline": outline, "turn": turn},
        )
    logger.debug("file_outline_upserted", path=path, session_id=session_id)


async def get_file_outline(execute, session_id: str, path: str) -> str | None:
    """Get a file's outline, with fallback matching on partial paths.

    First tries exact path match. If not found, normalizes path (Windows -> Unix),
    and retrieves outline for matching entries in a single query.
    """
    # Exact path match
    rows = await execute(
        "MATCH (fo:FileOutline {session_id: $session_id, path: $path}) RETURN fo.outline AS outline",
        {"session_id": session_id, "path": path},
    )
    if rows:
        return rows[0].get("outline") or None

    # Fallback: normalize and find matches
    norm_query = path.replace("\\", "/").lstrip("/")
    if not norm_query:
        return None

    # Get all outlines in single query, filter in-process
    all_outlines = await execute(
        "MATCH (fo:FileOutline {session_id: $session_id}) RETURN fo.path AS path, fo.outline AS outline",
        {"session_id": session_id},
    )
    for row in all_outlines:
        stored = row.get("path", "").replace("\\", "/").lstrip("/")
        if stored.endswith(norm_query) or norm_query.endswith(stored):
            return row.get("outline") or None
    return None


async def evict_stale_file_cache(execute, session_id: str, max_turns_age: int, max_entries: int) -> None:
    """Evict stale file cache entries based on TTL and max entry count.

    - Removes entries where last_updated_turn < (current_turn - max_turns_age)
    - If still over max_entries, evicts oldest entries by last_updated_turn
    """
    # Get current turn
    current_turn_rows = await execute(
        "MATCH (s:Session {session_id: $sid}) RETURN s.turn_number AS turn",
        {"sid": session_id},
    )
    if not current_turn_rows:
        return
    current_turn = current_turn_rows[0].get("turn", 0)

    # TTL-based eviction: delete entries older than max_turns_age
    cutoff_turn = current_turn - max_turns_age
    if cutoff_turn > 0:
        await execute(
            """
            MATCH (fc:FileContent {session_id: $sid})
            WHERE fc.last_updated_turn < $cutoff
            DELETE fc
            """,
            {"sid": session_id, "cutoff": cutoff_turn},
        )
        await execute(
            """
            MATCH (fo:FileOutline {session_id: $sid})
            WHERE fo.last_updated_turn < $cutoff
            DELETE fo
            """,
            {"sid": session_id, "cutoff": cutoff_turn},
        )
        logger.debug("file_cache_ttl_eviction", session_id=session_id, cutoff_turn=cutoff_turn)

    # Count entries and evict oldest if over limit
    count_rows = await execute(
        "MATCH (fc:FileContent {session_id: $sid}) RETURN count(fc) AS count",
        {"sid": session_id},
    )
    count = count_rows[0].get("count", 0) if count_rows else 0

    if count > max_entries:
        to_evict = count - max_entries
        await execute(
            """
            MATCH (fc:FileContent {session_id: $sid})
            WITH fc ORDER BY fc.last_updated_turn ASC LIMIT $limit
            DELETE fc
            """,
            {"sid": session_id, "limit": to_evict},
        )
        logger.debug("file_cache_lru_eviction", session_id=session_id, evicted_count=to_evict)

    # Evict outlines independently
    outline_count_rows = await execute(
        "MATCH (fo:FileOutline {session_id: $sid}) RETURN count(fo) AS count",
        {"sid": session_id},
    )
    outline_count = outline_count_rows[0].get("count", 0) if outline_count_rows else 0
    if outline_count > max_entries:
        outline_to_evict = outline_count - max_entries
        await execute(
            """
            MATCH (fo:FileOutline {session_id: $sid})
            WITH fo ORDER BY fo.last_updated_turn ASC LIMIT $limit
            DELETE fo
            """,
            {"sid": session_id, "limit": outline_to_evict},
        )
        logger.debug("file_outline_lru_eviction", session_id=session_id, evicted_count=outline_to_evict)
