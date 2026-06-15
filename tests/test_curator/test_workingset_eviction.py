"""Integration: ARC working set bounds the state.py caches (Phase 4)."""

from __future__ import annotations

import pytest

from archolith_proxy.curator.briefing import SessionBriefing
from archolith_proxy.curator.state import (
    CuratorSnapshot,
    cache_briefing,
    cache_snapshot,
    clear_briefing,
    get_briefing,
    set_persist_callback,
    set_working_set,
)
from archolith_proxy.curator.working_set import ARCWorkingSet


def _b(sid, turn=1):
    return SessionBriefing(session_id=sid, source_turn=turn, session_goal="g")


def _s(sid, turn=1):
    return CuratorSnapshot(
        curated_paths=(), retained_turn_numbers=None, context_summary="",
        tool_calls_used=0, turn_number=turn,
    )


@pytest.fixture(autouse=True)
def _isolate():
    from archolith_proxy.curator import state as st
    set_persist_callback(None)
    set_working_set(None)
    st._briefing_cache.clear()
    st._cache.clear()
    yield
    set_persist_callback(None)
    set_working_set(None)
    st._briefing_cache.clear()
    st._cache.clear()


def test_disabled_by_default_unbounded():
    # No working set registered -> caches grow unbounded (current behavior).
    for i in range(10):
        cache_briefing(f"s{i}", _b(f"s{i}"))
    from archolith_proxy.curator import state as st
    assert len(st._briefing_cache) == 10


def test_bounds_to_capacity_and_evicts():
    set_working_set(ARCWorkingSet(3))
    from archolith_proxy.curator import state as st
    for i in range(5):
        cache_briefing(f"s{i}", _b(f"s{i}"))
    # Never more than the cap resident.
    assert len(st._briefing_cache) == 3
    # Most recent survive; earliest evicted.
    assert get_briefing("s4") is not None
    assert get_briefing("s0") is None


def test_eviction_drops_both_caches_for_victim():
    set_working_set(ARCWorkingSet(2))
    from archolith_proxy.curator import state as st
    cache_briefing("a", _b("a"))
    cache_snapshot("a", _s("a"))
    cache_briefing("b", _b("b"))
    cache_snapshot("b", _s("b"))
    cache_briefing("c", _b("c"))  # admits c, evicts a (oldest)
    assert "a" not in st._briefing_cache
    assert "a" not in st._cache  # both caches dropped for the victim


def test_eviction_does_not_fire_persist_delete():
    # Eviction is memory pressure, not session end: the persisted row must stay.
    deletes = []
    set_persist_callback(lambda kind, sid, obj: deletes.append((kind, sid)) if kind == "delete" else None)
    set_working_set(ARCWorkingSet(2))
    cache_briefing("a", _b("a"))
    cache_briefing("b", _b("b"))
    cache_briefing("c", _b("c"))  # evicts a
    # No "delete" event for the evicted session.
    assert ("delete", "a") not in deletes


def test_clear_does_fire_persist_delete_and_removes_from_ws():
    deletes = []
    set_persist_callback(lambda kind, sid, obj: deletes.append((kind, sid)) if kind == "delete" else None)
    ws = ARCWorkingSet(3)
    set_working_set(ws)
    cache_briefing("a", _b("a"))
    clear_briefing("a")  # true session end
    assert ("delete", "a") in deletes
    assert "a" not in ws


def test_read_refreshes_recency_protecting_from_eviction():
    set_working_set(ARCWorkingSet(2))
    cache_briefing("a", _b("a"))
    cache_briefing("b", _b("b"))
    # Touch a so it is most-recently-used; admitting c should then evict b.
    assert get_briefing("a") is not None
    cache_briefing("c", _b("c"))
    assert get_briefing("a") is not None
    assert get_briefing("b") is None
