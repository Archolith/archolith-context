"""Agent-solo turn compression — thin proxy-side orchestrator.

Delegates all compression logic to ``archolith_rtk.agent_solo``.
This module owns:
- Per-session ``DedupeTracker`` lifecycle
- Curator prefix cache (persist curator savings across agent-solo turns)
- Config → RTK parameter mapping
- Stats dict formatting for trace recording

The RTK module owns HOW to compress; this module owns WHEN.
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Per-session DedupeTracker registry
# ---------------------------------------------------------------------------

_session_trackers: dict[str, Any] = {}
_MAX_SESSIONS = 200


def _get_tracker(session_id: str) -> Any:
    """Get or create a per-session DedupeTracker."""
    tracker = _session_trackers.get(session_id)
    if tracker is not None:
        return tracker

    try:
        from archolith_rtk.dedupe import DedupeTracker
    except ImportError:
        return None

    if len(_session_trackers) >= _MAX_SESSIONS:
        oldest = next(iter(_session_trackers))
        del _session_trackers[oldest]

    tracker = DedupeTracker()
    _session_trackers[session_id] = tracker
    return tracker


# ---------------------------------------------------------------------------
# Curator prefix cache — persist curator savings across agent-solo turns
# ---------------------------------------------------------------------------

class _CuratorCache:
    """Cached curator rewrite for a session.

    After the curator rewrites messages on a user turn, we store:
    - original_count: number of messages the curator received
    - fingerprint: hash of the last original message (boundary check)
    - rewritten: the curator's rewritten message list

    On subsequent agent-solo turns, if the incoming messages start with
    the same prefix (verified by count + fingerprint), we splice the
    cached rewrite in place of the original prefix.  New messages
    (model response + tool results) are appended unchanged.
    """
    __slots__ = ("original_count", "fingerprint", "rewritten")

    def __init__(self, original_count: int, fingerprint: str, rewritten: list[dict]):
        self.original_count = original_count
        self.fingerprint = fingerprint
        self.rewritten = rewritten


_curator_caches: dict[str, _CuratorCache] = {}


def _fingerprint_message(msg: dict) -> str:
    """Fast fingerprint of a message — role + first 200 chars of content."""
    content = msg.get("content") or ""
    if isinstance(content, list):
        # Content-block array (Anthropic format) — serialize minimally
        content = str(content)[:200]
    else:
        content = content[:200]
    raw = f"{msg.get('role', '')}:{content}"
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()


def cache_curator_rewrite(
    session_id: str,
    original_messages: list[dict],
    rewritten_messages: list[dict],
) -> None:
    """Cache the curator's rewrite for use by subsequent agent-solo turns.

    Called from chat.py after a successful curator rewrite.
    """
    if not original_messages or not rewritten_messages:
        return

    count = len(original_messages)
    fp = _fingerprint_message(original_messages[-1])
    _curator_caches[session_id] = _CuratorCache(
        original_count=count,
        fingerprint=fp,
        rewritten=[m for m in rewritten_messages],  # shallow copy
    )
    logger.debug(
        "curator_cache_stored",
        session_id=session_id,
        original_count=count,
        rewritten_count=len(rewritten_messages),
    )


def _apply_curator_prefix(
    session_id: str, messages: list[dict],
) -> tuple[list[dict], int]:
    """Try to splice cached curator rewrite into the message prefix.

    Returns (messages, chars_saved).  If no cache hit, returns the
    original messages unchanged with 0 chars saved.
    """
    cache = _curator_caches.get(session_id)
    if cache is None:
        return messages, 0

    # Must have at least as many messages as the original curator input
    if len(messages) < cache.original_count:
        return messages, 0

    # Fingerprint the boundary message — cheap O(1) safety check
    boundary_msg = messages[cache.original_count - 1]
    if _fingerprint_message(boundary_msg) != cache.fingerprint:
        # Prefix changed (different conversation, compaction, etc.) — invalidate
        logger.debug(
            "curator_cache_miss_fingerprint",
            session_id=session_id,
            expected=cache.fingerprint,
            actual=_fingerprint_message(boundary_msg),
        )
        del _curator_caches[session_id]
        return messages, 0

    # Splice: cached rewrite + new tail
    new_tail = messages[cache.original_count:]
    spliced = cache.rewritten + new_tail

    # Estimate chars saved
    original_prefix_chars = sum(
        len(messages[i].get("content") or "")
        for i in range(cache.original_count)
    )
    rewritten_prefix_chars = sum(
        len(m.get("content") or "") for m in cache.rewritten
    )
    chars_saved = max(0, original_prefix_chars - rewritten_prefix_chars)

    logger.debug(
        "curator_cache_hit",
        session_id=session_id,
        original_msgs=cache.original_count,
        rewritten_msgs=len(cache.rewritten),
        tail_msgs=len(new_tail),
        total_msgs=len(spliced),
        chars_saved=chars_saved,
    )

    return spliced, chars_saved


def clear_curator_cache(session_id: str) -> None:
    """Clear the curator cache for a session."""
    _curator_caches.pop(session_id, None)


def clear_session_hashes(session_id: str) -> None:
    """Clear dedup state for a session (e.g., on session end)."""
    _session_trackers.pop(session_id, None)
    _curator_caches.pop(session_id, None)


# ---------------------------------------------------------------------------
# Main compression entry point
# ---------------------------------------------------------------------------

def compress_agent_solo(
    messages: list[dict[str, Any]],
    session_id: str,
    input_tokens: int,
    *,
    shrink_enabled: bool = False,
    dedup_enabled: bool = False,
    compress_middle_enabled: bool = False,
    shrink_max_tokens: int = 2000,
    min_input_tokens: int = 30000,
    coherence_tail_size: int = 10,
    max_tail_messages: int = 20,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply enabled agent-solo compression strategies via RTK.

    First applies curator prefix cache (if available), then runs
    mechanical compression strategies on top.

    Returns (processed_messages, stats_dict) for trace recording.
    """
    stats: dict[str, Any] = {
        "strategies_applied": [],
        "chars_saved_shrink": 0,
        "chars_saved_dedup": 0,
        "chars_saved_middle": 0,
        "chars_saved_curator_cache": 0,
        "total_chars_saved": 0,
        "skipped_reason": None,
    }

    # Step 1: Apply curator prefix cache (always, even if strategies disabled)
    messages, curator_chars_saved = _apply_curator_prefix(session_id, messages)
    if curator_chars_saved > 0:
        stats["chars_saved_curator_cache"] = curator_chars_saved
        stats["total_chars_saved"] += curator_chars_saved
        stats["strategies_applied"].append("curator_cache")

    # Step 2: Apply mechanical compression strategies
    any_enabled = shrink_enabled or dedup_enabled or compress_middle_enabled
    if not any_enabled and curator_chars_saved == 0:
        stats["skipped_reason"] = "no_strategies_enabled"
        return messages, stats

    if any_enabled:
        # Re-estimate tokens after prefix splice
        effective_tokens = input_tokens - (curator_chars_saved // 4) if curator_chars_saved else input_tokens

        if effective_tokens < min_input_tokens and curator_chars_saved == 0:
            stats["skipped_reason"] = f"below_threshold_{input_tokens}<{min_input_tokens}"
            return messages, stats

        if effective_tokens >= min_input_tokens:
            try:
                from archolith_rtk.agent_solo import compress_agent_solo_turn
            except ImportError:
                if curator_chars_saved == 0:
                    stats["skipped_reason"] = "rtk_unavailable"
                return messages, stats

            tracker = _get_tracker(session_id) if dedup_enabled else None

            result = compress_agent_solo_turn(
                messages,
                dedup_tracker=tracker,
                shrink_enabled=shrink_enabled,
                dedup_enabled=dedup_enabled,
                filter_middle_enabled=compress_middle_enabled,
                shrink_max_tokens=shrink_max_tokens,
                coherence_tail_size=coherence_tail_size,
                tail_shrink_tokens=shrink_max_tokens,
            )

            rtk_stats = result.stats
            messages = result.messages

            # Merge RTK stats
            stats["strategies_applied"].extend(rtk_stats.strategies_applied)
            stats["chars_saved_shrink"] = rtk_stats.chars_saved_shrink
            stats["chars_saved_dedup"] = rtk_stats.chars_saved_dedup
            stats["chars_saved_middle"] = rtk_stats.chars_saved_filter
            stats["total_chars_saved"] += rtk_stats.total_chars_saved
            if rtk_stats.skipped_reason and not stats["strategies_applied"]:
                stats["skipped_reason"] = rtk_stats.skipped_reason

    return messages, stats
