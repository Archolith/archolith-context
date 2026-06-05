"""Regression tests for proxy in-memory leak fixes + resume recoverability.

Covers:
- TraceStore bg-pass per-session cap
- TraceStore session eviction drops bg-passes + metadata (not just turns)
- Resume after eviction rebuilds (turns + metadata repopulate)
- curator.pipeline.prune_last_attempts
- agent_solo._curator_caches hard cap
"""

from __future__ import annotations

from archolith_proxy.models.dtos import BackgroundPassTrace, TurnTrace
from archolith_proxy.trace.store import TraceStore


class TestBgPassBounds:
    async def test_bg_passes_capped_per_session(self) -> None:
        store = TraceStore(max_bg_passes_per_session=3)
        for _ in range(10):
            await store.record_bg_pass(BackgroundPassTrace(session_id="s1"))
        passes = await store.get_bg_passes("s1")
        assert len(passes) == 3  # only the most recent 3 retained

    async def test_eviction_drops_bg_passes_and_metadata(self) -> None:
        store = TraceStore(max_sessions=2)
        # Populate s1 with a turn, a bg-pass, and metadata
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        await store.record_bg_pass(BackgroundPassTrace(session_id="s1"))
        store.set_session_metadata("s1", "harness_env", {"AGENT": "x"})
        assert store.has_session_metadata("s1", "harness_env")

        # Push two more sessions → s1 (LRU) is evicted
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        await store.record(TurnTrace(session_id="s3", turn_number=1))

        assert await store.get_session_turns("s1") == []
        assert await store.get_bg_passes("s1") == []
        assert not store.has_session_metadata("s1", "harness_env")
        # Survivors intact
        assert await store.get_session_turns("s3") != []

    async def test_resume_after_eviction_rebuilds(self) -> None:
        store = TraceStore(max_sessions=2)
        await store.record(TurnTrace(session_id="s1", turn_number=1))
        store.set_session_metadata("s1", "harness_env", {"AGENT": "x"})
        await store.record(TurnTrace(session_id="s2", turn_number=1))
        await store.record(TurnTrace(session_id="s3", turn_number=1))  # evicts s1
        assert not store.has_session_metadata("s1", "harness_env")

        # s1 resumes: new turn + metadata repopulate cleanly
        await store.record(TurnTrace(session_id="s1", turn_number=2))
        store.set_session_metadata("s1", "harness_env", {"AGENT": "y"})
        assert await store.get_session_turns("s1") != []
        assert store.has_session_metadata("s1", "harness_env")
        assert store.get_session_metadata("s1", "harness_env") == {"AGENT": "y"}


class TestPruneLastAttempts:
    def test_prune_drops_inactive_keeps_active(self) -> None:
        from archolith_proxy.curator import pipeline

        pipeline._last_attempt.clear()
        pipeline._last_attempt["active"] = {"reason": "ok"}
        pipeline._last_attempt["abandoned"] = {"reason": "timeout"}

        pruned = pipeline.prune_last_attempts({"active"})

        assert pruned == 1
        assert "active" in pipeline._last_attempt
        assert "abandoned" not in pipeline._last_attempt
        pipeline._last_attempt.clear()


class TestCuratorCacheCap:
    def test_curator_cache_hard_capped(self) -> None:
        from archolith_proxy.proxy import agent_solo

        agent_solo._curator_caches.clear()
        msgs = [{"role": "user", "content": "x" * 50}]
        for i in range(agent_solo._MAX_SESSIONS + 25):
            agent_solo.cache_curator_rewrite(f"sess_{i}", msgs, msgs)
        assert len(agent_solo._curator_caches) <= agent_solo._MAX_SESSIONS
        agent_solo._curator_caches.clear()
