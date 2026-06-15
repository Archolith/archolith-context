"""Per-session curator state — caches last successful curator result + briefing.

The inline curator starts from scratch every turn, re-discovering the same
files and facts each time. This module caches the last successful result so
the curator prompt can say "you already fetched X, Y, Z — focus on what
changed" and cut 1-2 iterations off the typical run.

The two-pass curator extends this with a SessionBriefing cache: the background
pass writes a briefing, the inline pass reads it. Briefings are in-memory only
and considered fresh when source_turn >= current_turn - 1.

In-memory primary. When CURATOR_STATE_PERSIST_ENABLED is set, a write-through
callback (registered by curator.persistence at startup) mirrors the briefing and
snapshot caches to a durable sidecar so a warm restart can reload them. The
in-memory dict write stays synchronous and primary; persistence is async and
off the hot path. Disabled by default — then this module is in-memory only.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from archolith_proxy.curator.briefing import SessionBriefing


@dataclass(frozen=True)
class CuratorSnapshot:
    """Frozen snapshot of a successful curator run for the next turn."""
    curated_paths: tuple[str, ...]
    retained_turn_numbers: tuple[int, ...] | None
    context_summary: str          # truncated context block
    tool_calls_used: int
    turn_number: int
    timestamp: float = field(default_factory=time.time)


# session_id → last successful snapshot
# THREAD-SAFETY: safe under single asyncio event loop
_cache: dict[str, CuratorSnapshot] = {}

# session_id → last background pass briefing
# THREAD-SAFETY: safe under single asyncio event loop
_briefing_cache: dict[str, SessionBriefing] = {}

# Optional write-through persistence callback: cb(kind, session_id, obj) where
# kind is "briefing" | "snapshot" | "delete". Set by curator.persistence at
# startup when persistence is enabled; None means persistence is off (default).
_persist_cb = None


def set_persist_callback(cb) -> None:
    """Register (or clear with None) the write-through persistence callback."""
    global _persist_cb
    _persist_cb = cb


def _persist(kind: str, session_id: str, obj=None) -> None:
    """Fire the persistence callback if set; never let it break the hot path."""
    cb = _persist_cb
    if cb is None:
        return
    try:
        cb(kind, session_id, obj)
    except Exception:
        pass


def restore_caches(
    briefings: dict[str, SessionBriefing] | None,
    snapshots: dict[str, CuratorSnapshot] | None,
) -> None:
    """Repopulate the in-memory caches from persisted state at startup.

    Call BEFORE set_persist_callback so the restore itself is not re-persisted.
    """
    if briefings:
        _briefing_cache.update(briefings)
    if snapshots:
        _cache.update(snapshots)
    # Seed the working set (if active) with the restored sessions so the bound
    # applies from the first turn.
    if _working_set is not None:
        for sid in (*(briefings or {}), *(snapshots or {})):
            _touch_working_set(sid)


# ---------------------------------------------------------------------------
# ARC working set — adaptive recency+frequency bound on the session caches.
# None = disabled (default): caches are unbounded (current behavior). Set by
# main.py at startup when curator_workingset_enabled. Mirror of the persist hook.
# ---------------------------------------------------------------------------

_working_set = None


def set_working_set(ws) -> None:
    """Register (or clear with None) the ARC working set bounding the caches."""
    global _working_set
    _working_set = ws


def _touch_working_set(session_id: str) -> None:
    """Record an access; evict the cold victim's data from both caches if any.

    Eviction is memory pressure, NOT session end, so it pops the dicts directly
    and does NOT fire the persist delete — the persisted row stays so a later
    restart can still warm-start that session.
    """
    ws = _working_set
    if ws is None:
        return
    try:
        evicted = ws.record_access(session_id)
    except Exception:
        return
    if evicted is not None and evicted != session_id:
        _briefing_cache.pop(evicted, None)
        _cache.pop(evicted, None)
        try:
            from archolith_proxy.metrics import record_metric
            record_metric("curator_workingset_evictions", 1)
        except Exception:
            pass


def cache_snapshot(session_id: str, snapshot: CuratorSnapshot) -> None:
    """Store the latest curator snapshot for a session."""
    _cache[session_id] = snapshot
    _persist("snapshot", session_id, snapshot)
    _touch_working_set(session_id)


def get_snapshot(session_id: str) -> CuratorSnapshot | None:
    """Retrieve the cached curator snapshot, or None if absent."""
    snap = _cache.get(session_id)
    if snap is not None:
        _touch_working_set(session_id)  # refresh recency on a hit
    return snap


def clear_snapshot(session_id: str) -> None:
    """Remove cached snapshot for a session (e.g. on session end)."""
    _cache.pop(session_id, None)
    _persist("delete", session_id)
    if _working_set is not None:
        _working_set.remove(session_id)


# ---------------------------------------------------------------------------
# Briefing cache — two-pass curator
# ---------------------------------------------------------------------------

def cache_briefing(session_id: str, briefing: SessionBriefing) -> None:
    """Store the background pass briefing for a session."""
    _briefing_cache[session_id] = briefing
    _persist("briefing", session_id, briefing)
    _touch_working_set(session_id)


def get_briefing(session_id: str) -> SessionBriefing | None:
    """Retrieve the cached briefing, or None if absent."""
    briefing = _briefing_cache.get(session_id)
    if briefing is not None:
        _touch_working_set(session_id)  # refresh recency on a hit
    return briefing


def is_briefing_fresh(session_id: str, current_turn: int) -> bool:
    """Check if the cached briefing is fresh enough for the inline pass.

    A briefing is considered fresh when it was built after the previous turn
    (source_turn >= current_turn - 1). This means it was generated by the
    background pass that ran after the last user request.
    """
    briefing = _briefing_cache.get(session_id)
    if briefing is None:
        return False
    return briefing.source_turn >= current_turn - 1


def clear_briefing(session_id: str) -> None:
    """Remove cached briefing for a session."""
    _briefing_cache.pop(session_id, None)
    _persist("delete", session_id)
    if _working_set is not None:
        _working_set.remove(session_id)


# ---------------------------------------------------------------------------
# Background pass task guard — one in-flight per session
# ---------------------------------------------------------------------------

# session_id → in-flight asyncio.Task
# THREAD-SAFETY: safe under single asyncio event loop
_bg_tasks: dict[str, asyncio.Task] = {}


def swap_background_task(session_id: str, task: asyncio.Task) -> None:
    """Register a new background pass task, cancelling any in-flight one.

    Ensures at most one background pass runs per session at a time.
    Cancelled tasks see ``CancelledError`` and exit cleanly.
    """
    old = _bg_tasks.pop(session_id, None)
    if old is not None and not old.done():
        old.cancel()
        # Phase 0: count prepper passes cancelled by the next turn (cancel-and-lose).
        try:
            from archolith_proxy.metrics import record_metric
            record_metric("prepper_cancels", 1)
        except Exception:
            pass

    _bg_tasks[session_id] = task
    # Done callback only pops if the stored task IS the same object that completed.
    # This prevents an older task's callback from popping a newer task when
    # a new task is registered before the old one finishes.
    task.add_done_callback(lambda t: _bg_tasks.pop(session_id, None) if _bg_tasks.get(session_id) is t else None)


def cancel_background_task(session_id: str) -> None:
    """Cancel the in-flight background pass for a session, if any."""
    task = _bg_tasks.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()


def prune_session_state(active_session_ids: set[str]) -> int:
    """Drop curator state for sessions no longer present in the graph."""
    stale_ids = {
        sid for sid in (*list(_cache.keys()), *list(_briefing_cache.keys()), *list(_bg_tasks.keys()))
        if sid not in active_session_ids
    }
    for session_id in stale_ids:
        clear_snapshot(session_id)
        clear_briefing(session_id)
        cancel_background_task(session_id)
    return len(stale_ids)


__all__ = [
    "CuratorSnapshot",
    "cache_snapshot", "get_snapshot", "clear_snapshot",
    "cache_briefing", "get_briefing", "is_briefing_fresh", "clear_briefing",
    "swap_background_task", "cancel_background_task", "prune_session_state",
    "set_persist_callback", "restore_caches",
    "set_working_set",
]
