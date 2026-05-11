"""Per-session asyncio.Lock management for extraction ordering.

Turn N+1's assembly can read stale graph state if turn N's background
extraction hasn't committed yet. Session locks ensure that:

1. Before assembly, the handler waits for the prior extraction to finish
   (with a timeout fallback so requests don't hang indefinitely).
2. During extraction, the lock is held to prevent concurrent writes to
   the same session's graph data.

Locks are lightweight dicts — no external state, cleaned up on session
expiry or when the dict grows too large.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger()

# Module-level session lock registry
_session_locks: dict[str, asyncio.Lock] = {}


def get_session_lock(session_id: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a session."""
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    return _session_locks[session_id]


async def wait_for_prior_extraction(session_id: str, timeout_s: float = 5.0) -> bool:
    """Wait for any in-progress extraction to complete before assembly.

    Acquires the session lock with a timeout. If acquired, releases immediately —
    we just needed to ensure the prior write committed.

    Returns True if the lock was acquired (prior extraction done or no lock held).
    Returns False on timeout (proceed with potentially stale data, log warning).
    """
    lock = get_session_lock(session_id)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout_s)
        lock.release()
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "extraction_lock_timeout",
            session_id=session_id,
            timeout_s=timeout_s,
        )
        return False


def cleanup_session_lock(session_id: str) -> None:
    """Remove a session lock from the registry (call on session expiry)."""
    _session_locks.pop(session_id, None)


def cleanup_stale_locks(max_locks: int = 10000) -> int:
    """Remove excess locks to prevent unbounded memory growth.

    Only called when the lock registry exceeds max_locks.
    Removes the oldest half (by dict insertion order — Python 3.7+).
    """
    if len(_session_locks) <= max_locks:
        return 0
    keys = list(_session_locks.keys())
    to_remove = keys[: len(keys) // 2]
    for key in to_remove:
        del _session_locks[key]
    return len(to_remove)
