"""ARC working set — adaptive recency+frequency bound on the session caches (Phase 4).

The curator's in-memory caches (``state.py`` ``_briefing_cache`` / ``_cache``) are
only pruned when a session leaves the graph, so within a long-lived process they
grow with the number of sessions ever seen. This module bounds them with an
**Adaptive Replacement Cache** (ARC, Megiddo & Modha 2003): two LRU lists T1
(seen once / recency) and T2 (seen >=2 / frequency), plus ghost lists B1/B2 of
recently-evicted keys that adapt a target size ``p`` for T1. ARC self-tunes
between recency and frequency and outperforms plain LRU.

Here the "pages" are session ids; the data they guard lives in the state caches.
``record_access`` returns the session id whose data should be dropped from those
caches (or None). Existing keys are never evicted, so a read during a turn can
never evict the session being read.
"""

from __future__ import annotations

from collections import OrderedDict


class ARCWorkingSet:
    """Adaptive Replacement Cache over session-id keys (data held externally)."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.c = capacity
        self.p = 0  # adaptive target size for T1
        # Resident lists (value-less; data lives in the state caches).
        self.t1: OrderedDict[str, None] = OrderedDict()  # recent, seen once
        self.t2: OrderedDict[str, None] = OrderedDict()  # frequent, seen >= 2
        # Ghost lists (evicted keys, no data).
        self.b1: OrderedDict[str, None] = OrderedDict()  # ghosts evicted from T1
        self.b2: OrderedDict[str, None] = OrderedDict()  # ghosts evicted from T2

    # -- public API ---------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        return key in self.t1 or key in self.t2

    def __len__(self) -> int:
        return len(self.t1) + len(self.t2)

    def keys(self) -> list[str]:
        return [*self.t1.keys(), *self.t2.keys()]

    def record_access(self, key: str) -> str | None:
        """Register an access/insert for ``key``. Returns an evicted key or None.

        The returned key (if any) is a session whose data should now be dropped
        from the external caches — it has moved to a ghost list (or been
        discarded). The accessed ``key`` itself is never the eviction victim.
        """
        # Case I — resident hit: promote to T2 MRU (frequency). No eviction.
        if key in self.t1:
            del self.t1[key]
            self.t2[key] = None
            return None
        if key in self.t2:
            self.t2.move_to_end(key)
            return None

        # Case II — ghost hit in B1: adapt p up, replace, admit to T2.
        if key in self.b1:
            delta = 1 if len(self.b1) >= len(self.b2) else max(1, len(self.b2) // max(1, len(self.b1)))
            self.p = min(self.c, self.p + delta)
            evicted = self._replace(key)
            del self.b1[key]
            self.t2[key] = None
            return evicted

        # Case III — ghost hit in B2: adapt p down, replace, admit to T2.
        if key in self.b2:
            delta = 1 if len(self.b2) >= len(self.b1) else max(1, len(self.b1) // max(1, len(self.b2)))
            self.p = max(0, self.p - delta)
            evicted = self._replace(key)
            del self.b2[key]
            self.t2[key] = None
            return evicted

        # Case IV — true miss: admit a brand-new key to T1 MRU.
        evicted = None
        if len(self.t1) + len(self.b1) == self.c:
            # L1 (T1+B1) is full.
            if len(self.t1) < self.c:
                # Drop B1 LRU ghost, then replace a resident page.
                self.b1.popitem(last=False)
                evicted = self._replace(key)
            else:
                # B1 empty and T1 full: evict T1 LRU directly (no ghost kept).
                evicted, _ = self.t1.popitem(last=False)
        else:
            total = len(self.t1) + len(self.t2) + len(self.b1) + len(self.b2)
            if total >= self.c:
                if total == 2 * self.c:
                    # Directory full: drop B2 LRU ghost.
                    if self.b2:
                        self.b2.popitem(last=False)
                evicted = self._replace(key)

        self.t1[key] = None
        return evicted

    def remove(self, key: str) -> None:
        """Forget a key entirely (session end). Idempotent."""
        for d in (self.t1, self.t2, self.b1, self.b2):
            d.pop(key, None)

    # -- internals ----------------------------------------------------------

    def _replace(self, incoming: str) -> str | None:
        """ARC REPLACE: evict one resident page to a ghost list, return its key."""
        if self.t1 and (
            len(self.t1) > self.p
            or (incoming in self.b2 and len(self.t1) == self.p)
        ):
            # Evict T1 LRU -> B1 ghost.
            victim, _ = self.t1.popitem(last=False)
            self.b1[victim] = None
            return victim
        if self.t2:
            # Evict T2 LRU -> B2 ghost.
            victim, _ = self.t2.popitem(last=False)
            self.b2[victim] = None
            return victim
        if self.t1:
            victim, _ = self.t1.popitem(last=False)
            self.b1[victim] = None
            return victim
        return None


__all__ = ["ARCWorkingSet"]
