"""Agent-solo turn compression — thin proxy-side orchestrator.

Delegates all compression logic to ``archolith_rtk.agent_solo``.
This module owns:
- Per-session ``DedupeTracker`` lifecycle
- Config → RTK parameter mapping
- Stats dict formatting for trace recording

The RTK module owns HOW to compress; this module owns WHEN.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import structlog

logger = structlog.get_logger()


# Per-session DedupeTracker registry.  One tracker per session so that
# cross-turn dedup state is scoped to the session's attention window.
# Process-level — resets on restart, which is fine since dedup only
# matters within a live session.
_session_trackers: dict[str, Any] = {}

# Maximum number of tracked sessions before we stop creating new trackers.
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
        # Evict oldest session
        oldest = next(iter(_session_trackers))
        del _session_trackers[oldest]

    tracker = DedupeTracker()
    _session_trackers[session_id] = tracker
    return tracker


def clear_session_hashes(session_id: str) -> None:
    """Clear dedup state for a session (e.g., on session end)."""
    _session_trackers.pop(session_id, None)


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

    Returns (processed_messages, stats_dict) for trace recording.
    """
    stats: dict[str, Any] = {
        "strategies_applied": [],
        "chars_saved_shrink": 0,
        "chars_saved_dedup": 0,
        "chars_saved_middle": 0,
        "total_chars_saved": 0,
        "skipped_reason": None,
    }

    any_enabled = shrink_enabled or dedup_enabled or compress_middle_enabled
    if not any_enabled:
        stats["skipped_reason"] = "no_strategies_enabled"
        return messages, stats

    if input_tokens < min_input_tokens:
        stats["skipped_reason"] = f"below_threshold_{input_tokens}<{min_input_tokens}"
        return messages, stats

    try:
        from archolith_rtk.agent_solo import compress_agent_solo_turn
    except ImportError:
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

    # Map RTK stats to the proxy trace format
    rtk_stats = result.stats
    stats["strategies_applied"] = rtk_stats.strategies_applied
    stats["chars_saved_shrink"] = rtk_stats.chars_saved_shrink
    stats["chars_saved_dedup"] = rtk_stats.chars_saved_dedup
    stats["chars_saved_middle"] = rtk_stats.chars_saved_filter
    stats["total_chars_saved"] = rtk_stats.total_chars_saved
    stats["skipped_reason"] = rtk_stats.skipped_reason

    return result.messages, stats
