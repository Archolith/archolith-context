"""Tests for D9 — O(1) LRU session eviction via OrderedDict.

Eviction semantics must be identical to the previous list-based order:
least-recently-active session is evicted first, a touch promotes a session to
most-recently-active, and the current session is never evicted.
"""

from __future__ import annotations

from collections import OrderedDict

from archolith_proxy.models.dtos import TurnTrace
from archolith_proxy.trace.store import TraceStore


class TestLruEvictionOrder:
    def test_session_order_is_ordereddict(self):
        store = TraceStore()
        assert isinstance(store._session_order, OrderedDict)

    async def test_oldest_session_evicted_first(self):
        store = TraceStore(max_sessions=2)
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        await store.record(TurnTrace(session_id="s3", turn_number=1))  # evicts s1
        assert await store.get_session_turns("s1") == []
        assert await store.get_session_turns("s2") != []
        assert await store.get_session_turns("s3") != []

    async def test_touch_promotes_to_mru_and_protects(self):
        store = TraceStore(max_sessions=2)
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        # Touch s1 -> s1 becomes MRU, s2 becomes LRU.
        await store.record(TurnTrace(session_id="s1", turn_number=2))
        # New session evicts the LRU, which is now s2 (not s1).
        await store.record(TurnTrace(session_id="s3", turn_number=1))
        assert await store.get_session_turns("s1") != []
        assert await store.get_session_turns("s2") == []
        assert await store.get_session_turns("s3") != []

    async def test_current_session_not_self_evicted(self):
        store = TraceStore(max_sessions=1)
        # Repeated records of the same session must not evict itself.
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record(TurnTrace(session_id="s1", turn_number=2))
        await store.record(TurnTrace(session_id="s1", turn_number=3))
        assert await store.get_session_turns("s1") != []

    async def test_order_tracks_only_live_sessions(self):
        store = TraceStore(max_sessions=2)
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        await store.record(TurnTrace(session_id="s3", turn_number=1))  # evicts s1
        # Evicted session is removed from the order map (no unbounded growth).
        assert "s1" not in store._session_order
        assert set(store._session_order.keys()) == {"s2", "s3"}
