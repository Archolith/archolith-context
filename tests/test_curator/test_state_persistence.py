"""Tests for curator state snapshot durability (Phase 3, slice 1)."""

from __future__ import annotations

import pytest

from archolith_proxy.curator.briefing import PreFetchedFile, SessionBriefing
from archolith_proxy.curator.persistence import (
    StatePersistence,
    briefing_from_dict,
    briefing_to_dict,
    reset_state_persistence,
    snapshot_from_dict,
    snapshot_to_dict,
)
from archolith_proxy.curator.state import (
    CuratorSnapshot,
    cache_briefing,
    cache_snapshot,
    clear_briefing,
    get_briefing,
    get_snapshot,
    restore_caches,
    set_persist_callback,
)


def _make_briefing(session_id: str = "s1", source_turn: int = 5) -> SessionBriefing:
    return SessionBriefing(
        session_id=session_id,
        source_turn=source_turn,
        checkpoint_text="cp",
        open_issues_text="oi",
        last_verification_text="lv",
        decisions_text="dec",
        session_goal="goal",
        facts_text="facts",
        files=[
            PreFetchedFile(
                path="a.py",
                outline="line 1: def f",
                sections=[(1, 20, "code-a"), (30, 40, "code-b")],
                relevance="fetched",
            ),
        ],
        retained_turns=[3, 5],
        context_block="=== KEY FACTS ===\nfacts",
        mode="two_curator",
        tool_calls_used=4,
        iterations_used=3,
        latency_ms=123.4,
    )


def _make_snapshot(session_id: str = "s1", turn: int = 5) -> CuratorSnapshot:
    return CuratorSnapshot(
        curated_paths=("a.py", "b.py"),
        retained_turn_numbers=(3, 5),
        context_summary="summary",
        tool_calls_used=2,
        turn_number=turn,
    )


@pytest.fixture(autouse=True)
def _isolate_state():
    """Reset module globals + singleton around every test."""
    from archolith_proxy.curator import state as state_mod

    set_persist_callback(None)
    state_mod._briefing_cache.clear()
    state_mod._cache.clear()
    reset_state_persistence()
    yield
    set_persist_callback(None)
    state_mod._briefing_cache.clear()
    state_mod._cache.clear()
    reset_state_persistence()


# ---------------------------------------------------------------------------
# Serialization round-trips
# ---------------------------------------------------------------------------

def test_briefing_roundtrip_preserves_nested_files():
    b = _make_briefing()
    restored = briefing_from_dict(briefing_to_dict(b))
    assert restored == b
    # tuples inside sections survive as tuples
    assert restored.files[0].sections == [(1, 20, "code-a"), (30, 40, "code-b")]


def test_snapshot_roundtrip_preserves_tuples():
    s = _make_snapshot()
    restored = snapshot_from_dict(snapshot_to_dict(s))
    assert restored == s
    assert isinstance(restored.curated_paths, tuple)
    assert restored.retained_turn_numbers == (3, 5)


def test_snapshot_roundtrip_none_retained():
    s = CuratorSnapshot(
        curated_paths=(),
        retained_turn_numbers=None,
        context_summary="",
        tool_calls_used=0,
        turn_number=1,
    )
    assert snapshot_from_dict(snapshot_to_dict(s)) == s


# ---------------------------------------------------------------------------
# Store + reload
# ---------------------------------------------------------------------------

async def test_store_and_load_all_reconstructs(tmp_path):
    db = str(tmp_path / "state.db")
    b = _make_briefing("s1", 5)
    s = _make_snapshot("s1", 5)
    sp = StatePersistence(db)
    await sp.start()
    sp.enqueue("briefing", "s1", b)
    sp.enqueue("snapshot", "s1", s)
    await sp.stop()  # flushes

    sp2 = StatePersistence(db)
    await sp2.start()
    briefings, snapshots = await sp2.load_all()
    await sp2.stop()

    assert set(briefings) == {"s1"}
    assert briefings["s1"] == b
    assert snapshots["s1"] == s


async def test_delete_removes_persisted_rows(tmp_path):
    db = str(tmp_path / "state.db")
    sp = StatePersistence(db)
    await sp.start()
    sp.enqueue("briefing", "s1", _make_briefing("s1", 5))
    sp.enqueue("snapshot", "s1", _make_snapshot("s1", 5))
    sp.enqueue("delete", "s1", None)
    await sp.stop()

    sp2 = StatePersistence(db)
    await sp2.start()
    briefings, snapshots = await sp2.load_all()
    await sp2.stop()
    assert briefings == {}
    assert snapshots == {}


async def test_upsert_keeps_latest(tmp_path):
    db = str(tmp_path / "state.db")
    sp = StatePersistence(db)
    await sp.start()
    sp.enqueue("briefing", "s1", _make_briefing("s1", 5))
    sp.enqueue("briefing", "s1", _make_briefing("s1", 9))
    await sp.stop()

    sp2 = StatePersistence(db)
    await sp2.start()
    briefings, _ = await sp2.load_all()
    await sp2.stop()
    assert briefings["s1"].source_turn == 9


# ---------------------------------------------------------------------------
# Write-through integration via state.py
# ---------------------------------------------------------------------------

async def test_write_through_and_restore(tmp_path):
    db = str(tmp_path / "state.db")
    b = _make_briefing("s1", 7)
    s = _make_snapshot("s1", 7)
    sp = StatePersistence(db)
    await sp.start()
    set_persist_callback(sp.enqueue)

    cache_briefing("s1", b)
    cache_snapshot("s1", s)
    clear_briefing("s2")  # delete on an absent session is harmless

    await sp.stop()
    set_persist_callback(None)

    # Simulate a restart: clear in-memory, reload from db, restore caches.
    from archolith_proxy.curator import state as state_mod
    state_mod._briefing_cache.clear()
    state_mod._cache.clear()
    assert get_briefing("s1") is None

    sp2 = StatePersistence(db)
    await sp2.start()
    briefings, snapshots = await sp2.load_all()
    await sp2.stop()
    restore_caches(briefings, snapshots)

    assert get_briefing("s1") == b
    assert get_snapshot("s1") == s


def test_disabled_by_default_no_persist_no_error():
    # No callback registered (default). Caching must not raise and must not
    # attempt persistence.
    cache_briefing("s1", _make_briefing("s1", 1))
    cache_snapshot("s1", _make_snapshot("s1", 1))
    assert get_briefing("s1") is not None
