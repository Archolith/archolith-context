"""Tests for the ARC working set (Phase 4)."""

from __future__ import annotations

import pytest

from archolith_proxy.curator.working_set import ARCWorkingSet


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        ARCWorkingSet(0)


def test_admits_up_to_capacity_without_eviction():
    ws = ARCWorkingSet(3)
    assert ws.record_access("a") is None
    assert ws.record_access("b") is None
    assert ws.record_access("c") is None
    assert len(ws) == 3
    assert set(ws.keys()) == {"a", "b", "c"}


def test_capacity_never_exceeded_and_one_evicted():
    ws = ARCWorkingSet(3)
    for k in ("a", "b", "c"):
        ws.record_access(k)
    evicted = ws.record_access("d")  # 4th distinct -> must evict one
    assert evicted is not None
    assert evicted in {"a", "b", "c"}
    assert len(ws) == 3
    assert "d" in ws
    assert evicted not in ws


def test_existing_key_access_never_evicts():
    ws = ARCWorkingSet(2)
    ws.record_access("a")
    ws.record_access("b")
    # Re-access of resident keys: no eviction, ever.
    for _ in range(10):
        assert ws.record_access("a") is None
        assert ws.record_access("b") is None
    assert len(ws) == 2


def test_lru_victim_under_recency():
    ws = ARCWorkingSet(2)
    ws.record_access("a")
    ws.record_access("b")
    ws.record_access("a")  # a is now MRU; b is LRU in T1... a promoted to T2
    evicted = ws.record_access("c")
    # b (least recently used, still in T1) should be the victim, not a.
    assert evicted == "b"
    assert "a" in ws and "c" in ws


def test_capacity_invariant_under_churn():
    ws = ARCWorkingSet(5)
    for i in range(100):
        ws.record_access(f"s{i % 12}")
        assert len(ws) <= 5
    assert len(ws) == 5


def test_remove_is_idempotent_and_frees_slot():
    ws = ARCWorkingSet(2)
    ws.record_access("a")
    ws.record_access("b")
    ws.remove("a")
    ws.remove("a")  # idempotent
    assert "a" not in ws
    assert len(ws) == 1
    # A new key now fits without eviction.
    assert ws.record_access("c") is None
    assert len(ws) == 2


def test_ghost_hit_adapts_p():
    ws = ARCWorkingSet(2)
    ws.record_access("a")
    ws.record_access("a")  # a -> T2 (frequent)
    ws.record_access("b")  # b -> T1
    ws.record_access("c")  # over budget: evict b (T1 LRU) -> B1 ghost; c -> T1
    assert "b" not in ws
    p_before = ws.p
    ws.record_access("b")  # ghost hit in B1 -> p increases, b readmitted to T2
    assert ws.p > p_before
    assert "b" in ws
    assert len(ws) == 2


def test_frequency_survives_recency_pressure():
    ws = ARCWorkingSet(3)
    ws.record_access("hot")
    ws.record_access("hot")  # promote to T2 (frequent)
    # Churn three new recency-only keys through the cache.
    for k in ("x", "y", "z", "w"):
        ws.record_access(k)
    # The frequently-used key should still be resident (ARC protects T2).
    assert "hot" in ws
