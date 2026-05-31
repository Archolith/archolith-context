"""Per-session curator state — caches last successful curator result.

The inline curator starts from scratch every turn, re-discovering the same
files and facts each time. This module caches the last successful result so
the curator prompt can say "you already fetched X, Y, Z — focus on what
changed" and cut 1-2 iterations off the typical run.

In-memory only — lost on restart, which is fine since the curator rebuilds
from the graph/cache backend anyway.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


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
_cache: dict[str, CuratorSnapshot] = {}


def cache_snapshot(session_id: str, snapshot: CuratorSnapshot) -> None:
    """Store the latest curator snapshot for a session."""
    _cache[session_id] = snapshot


def get_snapshot(session_id: str) -> CuratorSnapshot | None:
    """Retrieve the cached curator snapshot, or None if absent."""
    return _cache.get(session_id)


def clear_snapshot(session_id: str) -> None:
    """Remove cached snapshot for a session (e.g. on session end)."""
    _cache.pop(session_id, None)
