"""Tests for D10 — metrics endpoint computes trace-derived metrics from one snapshot.

All trace-store reads in /metrics must happen under a single lock acquisition so
derived ratios are not mixed across a concurrent mutation.
"""

from __future__ import annotations

import asyncio

import pytest

from archolith_proxy.config import reset_settings
from archolith_proxy.models.dtos import TurnTrace
from archolith_proxy.trace.store import TraceStore


class _CountingLock:
    """Wrap an asyncio.Lock and count how many times it is acquired."""

    def __init__(self) -> None:
        self._inner = asyncio.Lock()
        self.acquires = 0

    async def __aenter__(self):
        self.acquires += 1
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


class TestMetricsSnapshotConsistency:
    def setup_method(self):
        from archolith_proxy.graph.backend import reset_backend
        reset_backend()
        reset_settings()

    @pytest.mark.asyncio
    async def test_metrics_uses_single_lock_acquisition(self, app, client):
        store = TraceStore()
        await store.record(TurnTrace(
            session_id="s1", turn_number=1, user_turn_count=2,
            output_tokens=10, cache_hit_tokens=3, cache_miss_tokens=4,
            assembly_mode="curator", assembly_latency_ms=12.0,
        ))
        await store.record(TurnTrace(
            session_id="s2", turn_number=1, user_turn_count=5,
            output_tokens=20, cache_hit_tokens=1, cache_miss_tokens=2,
        ))

        # Wrap the lock AFTER seeding so only the endpoint's reads are counted.
        counting = _CountingLock()
        store._lock = counting
        app.state.trace_store = store

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        # All trace-store reads happened under exactly one acquisition (D10).
        assert counting.acquires == 1

        data = resp.json()
        assert data["user_turns_by_session"] == {"s1": 2, "s2": 5}
