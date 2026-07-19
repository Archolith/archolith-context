"""Context cache helpers for prompt cache stability (Phase 0).

This module provides the core functions for the append-only context cache:
- compute_context_signature
- get_cached_context
- store_context

These are intentionally lightweight and can be used by both the deterministic
assembler and the full curator path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


def compute_context_signature(
    session_goal: str,
    touched_files: list[str],
    user_message: str,
    briefing_hash: str | None = None,
) -> str:
    """Compute a stable signature for context caching.

    The signature is designed to be stable enough for high cache hit rates
    while still capturing meaningful changes in session state.
    """
    key_parts = [
        session_goal or "",
        ",".join(sorted(touched_files)) if touched_files else "",
        (user_message or "")[:200],
    ]
    if briefing_hash:
        key_parts.append(briefing_hash)

    key = "|".join(key_parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_cached_context(
    db_path: str,
    session_id: str,
    signature: str,
    max_age_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Retrieve a cached context block if it exists and is fresh enough.

    Returns None if no matching entry is found or if it has expired.
    """
    if not Path(db_path).exists():
        return None

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT rendered_block, files_selected_json, created_turn, last_used_at
            FROM context_cache
            WHERE session_id = ? AND signature = ?
            """,
            (session_id, signature),
        ).fetchone()

        if not row:
            return None

        # Optional age check (provider TTL enforcement)
        if max_age_seconds is not None:
            age = time.time() - row["last_used_at"]
            if age > max_age_seconds:
                return None

        # Update last_used_at (best-effort)
        conn.execute(
            "UPDATE context_cache SET last_used_at = ? WHERE session_id = ? AND signature = ?",
            (time.time(), session_id, signature),
        )
        conn.commit()
        conn.close()

        rendered_block = row["rendered_block"]
        estimated_tokens = len(rendered_block) // 4

        return {
            "rendered_block": rendered_block,
            "files_selected": json.loads(row["files_selected_json"] or "[]"),
            "created_turn": row["created_turn"],
            "estimated_tokens": estimated_tokens,
        }

    except Exception as e:
        logger.warning("context_cache_read_failed", error=str(e), session_id=session_id)
        return None


def store_context(
    db_path: str,
    session_id: str,
    signature: str,
    rendered_block: str,
    files_selected: list[dict],
    created_turn: int,
    is_cold_start: bool = False,
) -> bool:
    """Store a rendered context block in the append-only cache."""
    try:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)

        conn.execute(
            """
            INSERT INTO context_cache
                (session_id, signature, rendered_block, files_selected_json,
                 created_turn, last_used_at, is_cold_start)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, signature) DO UPDATE SET
                rendered_block = excluded.rendered_block,
                files_selected_json = excluded.files_selected_json,
                last_used_at = excluded.last_used_at
            """,
            (
                session_id,
                signature,
                rendered_block,
                json.dumps(files_selected),
                created_turn,
                time.time(),
                1 if is_cold_start else 0,
            ),
        )
        conn.commit()
        conn.close()
        return True

    except Exception as e:
        logger.warning("context_cache_write_failed", error=str(e), session_id=session_id)
        return False


def should_use_cached_context(
    cached_tokens: int,
    estimated_fresh_tokens: int,
    max_bloat_ratio: float = 1.6,
) -> bool:
    """
    Decide whether to use a cached context block or force a fresh render.

    Returns False (force refresh) if the cached version is significantly
    larger than what a fresh render would produce.
    """
    if estimated_fresh_tokens <= 0:
        return True  # Can't compare, prefer cache

    ratio = cached_tokens / estimated_fresh_tokens
    return ratio <= max_bloat_ratio


__all__ = [
    "compute_context_signature",
    "get_cached_context",
    "store_context",
    "should_use_cached_context",
]