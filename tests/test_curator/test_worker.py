"""Unit tests for the CuratorWorker and WorkerRegistry event-driven model.

Tests cover:
1. Debounce/coalesce behavior — burst of events coalesces to latest
2. No-cancel semantics — in-flight pass is never interrupted by new events
3. Idle-gate behavior — no pass runs without events
4. Registry lifecycle — lazy worker creation, active_count tracking
5. Shutdown semantics — idle eviction, session shutdown, full shutdown
6. Backpressure handling — enqueue never blocks or raises
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Ensure openai stub is available for all imports
# ---------------------------------------------------------------------------

def _ensure_openai_stub() -> None:
    if "openai" not in sys.modules or not hasattr(sys.modules["openai"], "AsyncOpenAI"):
        stub = types.ModuleType("openai")
        stub.AsyncOpenAI = MagicMock()
        stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
        stub.APITimeoutError = type("APITimeoutError", (Exception,), {})
        stub.InternalServerError = type("InternalServerError", (Exception,), {})
        stub.RateLimitError = type("RateLimitError", (Exception,), {})
        sys.modules["openai"] = stub


_ensure_openai_stub()

# Now safe to import curator modules
from archolith_proxy.curator.worker import (  # noqa: E402
    SessionEvent,
    CuratorWorker,
    WorkerRegistry,
    get_worker_registry,
    enqueue_curator_event,
    shutdown_idle_curator_workers,
    shutdown_all_curator_workers,
)


# ============================================================================
# 1: DEBOUNCE/COALESCE TEST
# ============================================================================

@pytest.mark.asyncio
async def test_debounce_coalesces_burst_to_latest():
    """Enqueueing multiple events in a burst coalesces to exactly one pass with latest event."""
    worker = CuratorWorker("test_session", debounce_ms=20, max_queue=100)
    worker.start()

    try:
        # Mock run_background_pass
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock) as mock_pass:
            # Enqueue three events in rapid succession
            event1 = SessionEvent(session_id="test_session", turn_number=1, user_message="msg1")
            event2 = SessionEvent(session_id="test_session", turn_number=2, user_message="msg2")
            event3 = SessionEvent(session_id="test_session", turn_number=3, user_message="msg3")

            worker.enqueue(event1)
            worker.enqueue(event2)
            worker.enqueue(event3)

            # Wait for debounce + processing
            await asyncio.sleep(0.15)

            # Should have run exactly once with event3's data
            assert mock_pass.await_count == 1
            call_kwargs = mock_pass.call_args[1]
            assert call_kwargs["turn_number"] == 3
            assert call_kwargs["user_message"] == "msg3"
            assert worker.passes_run == 1
    finally:
        await worker.aclose()


# ============================================================================
# 2: NO-CANCEL TEST (IN-FLIGHT PASS NOT CANCELLED)
# ============================================================================

@pytest.mark.asyncio
async def test_in_flight_pass_not_cancelled():
    """While a pass is in-flight, new events queue without cancelling the first pass."""
    worker = CuratorWorker("test_session", debounce_ms=5, max_queue=100)
    worker.start()

    try:
        # Block the pass mid-execution with an Event
        block_event = asyncio.Event()

        async def blocked_pass(**kwargs):
            await block_event.wait()

        with patch("archolith_proxy.curator.pipeline.run_background_pass", side_effect=blocked_pass):
            # Enqueue first event
            event1 = SessionEvent(session_id="test_session", turn_number=1, user_message="msg1")
            worker.enqueue(event1)

            # Let it start (short sleep > debounce window)
            await asyncio.sleep(0.1)

            # Now enqueue a second event while first is blocked
            event2 = SessionEvent(session_id="test_session", turn_number=2, user_message="msg2")
            worker.enqueue(event2)

            # Give the loop a chance to see the new event (it shouldn't cancel the running pass)
            await asyncio.sleep(0.05)

            # Unblock the first pass
            block_event.set()

            # Wait for both passes to complete
            await asyncio.sleep(0.2)

            # Should have run exactly 2 passes (first completes normally, second runs after)
            assert worker.passes_run == 2
    finally:
        await worker.aclose()


# ============================================================================
# 3: IDLE-GATE TEST
# ============================================================================

@pytest.mark.asyncio
async def test_idle_gate_no_pass_without_events():
    """A freshly started worker with no events runs no pass."""
    worker = CuratorWorker("test_session", debounce_ms=20, max_queue=100)
    worker.start()

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock) as mock_pass:
            # Don't enqueue anything, just wait a bit
            await asyncio.sleep(0.1)

            # Pass should not have been called
            assert mock_pass.await_count == 0
            assert worker.passes_run == 0
    finally:
        await worker.aclose()


# ============================================================================
# 4: SESSION_END EVENT TEST
# ============================================================================

@pytest.mark.asyncio
async def test_session_end_stops_drain_loop():
    """A session_end event stops the drain loop."""
    worker = CuratorWorker("test_session", debounce_ms=10, max_queue=100)
    worker.start()

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Enqueue a normal event
            event1 = SessionEvent(session_id="test_session", turn_number=1, user_message="msg1", kind="turn")
            worker.enqueue(event1)

            await asyncio.sleep(0.1)
            assert worker.passes_run == 1

            # Now enqueue a session_end
            end_event = SessionEvent(session_id="test_session", kind="session_end")
            worker.enqueue(end_event)

            await asyncio.sleep(0.1)

            # Task should have completed (no exception)
            assert worker._task is not None
            assert worker._task.done()

            # No additional pass should have been run
            assert worker.passes_run == 1
    finally:
        await worker.aclose()


# ============================================================================
# 5: REGISTRY LAZY CREATION TEST
# ============================================================================

@pytest.mark.asyncio
async def test_registry_lazy_creates_workers():
    """WorkerRegistry lazily creates a worker on first enqueue for a session."""
    registry = WorkerRegistry(debounce_ms=10, max_queue=100)

    try:
        assert registry.active_count == 0

        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Enqueue for session 1
            event1 = SessionEvent(session_id="session_1", turn_number=1, user_message="msg1")
            registry.enqueue(event1)
            assert registry.active_count == 1

            # Enqueue for session 2
            event2 = SessionEvent(session_id="session_2", turn_number=1, user_message="msg1")
            registry.enqueue(event2)
            assert registry.active_count == 2

            # Enqueue for session 1 again (no new worker)
            event3 = SessionEvent(session_id="session_1", turn_number=2, user_message="msg2")
            registry.enqueue(event3)
            assert registry.active_count == 2
    finally:
        await registry.shutdown_all()


# ============================================================================
# 6: REGISTRY SHUTDOWN_IDLE TEST
# ============================================================================

@pytest.mark.asyncio
async def test_registry_shutdown_idle():
    """WorkerRegistry.shutdown_idle(ttl) evicts workers idle longer than ttl."""
    registry = WorkerRegistry(debounce_ms=5, max_queue=100)

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Create two workers
            event1 = SessionEvent(session_id="session_1", turn_number=1, user_message="msg1")
            registry.enqueue(event1)

            event2 = SessionEvent(session_id="session_2", turn_number=1, user_message="msg1")
            registry.enqueue(event2)

            assert registry.active_count == 2

            # Let both workers age past the eviction threshold.
            await asyncio.sleep(0.15)

            # Refresh ONLY session_2 right before eviction so it is clearly fresh
            # while session_1 stays stale — makes the selective eviction
            # deterministic (no reliance on the gap between the two enqueues).
            registry.enqueue(SessionEvent(session_id="session_2", turn_number=2, user_message="msg2"))

            evicted = await registry.shutdown_idle(0.1)
            assert evicted == 1
            assert registry.active_count == 1
    finally:
        await registry.shutdown_all()


# ============================================================================
# 7: REGISTRY SHUTDOWN_SESSION TEST
# ============================================================================

@pytest.mark.asyncio
async def test_registry_shutdown_session():
    """WorkerRegistry.shutdown_session removes a specific worker."""
    registry = WorkerRegistry(debounce_ms=10, max_queue=100)

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Create two workers
            event1 = SessionEvent(session_id="session_1", turn_number=1, user_message="msg1")
            registry.enqueue(event1)

            event2 = SessionEvent(session_id="session_2", turn_number=1, user_message="msg1")
            registry.enqueue(event2)

            assert registry.active_count == 2

            # Shutdown session 1
            await registry.shutdown_session("session_1")
            assert registry.active_count == 1

            # Shutdown nonexistent session (no error)
            await registry.shutdown_session("session_3")
            assert registry.active_count == 1
    finally:
        await registry.shutdown_all()


# ============================================================================
# 8: REGISTRY SHUTDOWN_ALL TEST
# ============================================================================

@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.asyncio
async def test_registry_shutdown_all():
    """WorkerRegistry.shutdown_all stops all workers."""
    registry = WorkerRegistry(debounce_ms=10, max_queue=100)

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Create multiple workers
            for i in range(3):
                event = SessionEvent(session_id=f"session_{i}", turn_number=1, user_message="msg")
                registry.enqueue(event)

            assert registry.active_count == 3

            await registry.shutdown_all()
            assert registry.active_count == 0
    finally:
        pass


# ============================================================================
# 9: BACKPRESSURE HANDLING TEST
# ============================================================================

@pytest.mark.asyncio
async def test_backpressure_enqueue_never_blocks():
    """Enqueueing on a full queue coalesces without raising."""
    worker = CuratorWorker("test_session", debounce_ms=50, max_queue=2)

    try:
        # Don't start the worker so the queue fills up
        # Enqueue more than max_queue without error
        for i in range(5):
            event = SessionEvent(session_id="test_session", turn_number=i, user_message=f"msg{i}")
            worker.enqueue(event)  # Should not raise

        # Queue should be bounded (max_queue=2, but coalesced behavior)
        assert worker._queue.qsize() <= 2
    finally:
        await worker.aclose()


# ============================================================================
# 10: EVENTS_COALESCED COUNTER TEST
# ============================================================================

@pytest.mark.asyncio
async def test_events_coalesced_counter():
    """Coalesced events increment the events_coalesced counter."""
    worker = CuratorWorker("test_session", debounce_ms=20, max_queue=100)
    worker.start()

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Enqueue multiple events
            for i in range(4):
                event = SessionEvent(session_id="test_session", turn_number=i, user_message=f"msg{i}")
                worker.enqueue(event)

            await asyncio.sleep(0.15)

            # Should have coalesced 3 events (enqueued 4, but 1 was the initial)
            assert worker.events_coalesced >= 3
            assert worker.passes_run == 1
    finally:
        await worker.aclose()


# ============================================================================
# 11: LAST_ACTIVE TRACKING TEST
# ============================================================================

@pytest.mark.asyncio
async def test_last_active_tracking():
    """last_active is updated on enqueue and pass completion."""
    worker = CuratorWorker("test_session", debounce_ms=10, max_queue=100)

    initial = worker.last_active

    # Enqueue updates last_active
    event = SessionEvent(session_id="test_session", turn_number=1, user_message="msg1")
    await asyncio.sleep(0.01)
    worker.enqueue(event)
    after_enqueue = worker.last_active

    assert after_enqueue >= initial

    # Pass execution also updates last_active
    worker.start()
    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            await asyncio.sleep(0.15)
            after_pass = worker.last_active
            assert after_pass >= after_enqueue
    finally:
        await worker.aclose()


# ============================================================================
# 12: MODULE-LEVEL SINGLETON HELPERS TEST
# ============================================================================

@pytest.mark.asyncio
async def test_module_singleton_enqueue():
    """enqueue_curator_event uses the module-level singleton registry."""
    try:
        # Reset global registry
        await shutdown_all_curator_workers()

        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Use the module helper
            enqueue_curator_event(
                session_id="test_singleton",
                turn_number=1,
                user_message="test",
                session_goal="goal",
                messages=[],
            )

            # Registry should have created a worker
            registry = get_worker_registry()
            assert registry.active_count == 1

            await asyncio.sleep(0.05)
    finally:
        await shutdown_all_curator_workers()


# ============================================================================
# 13: MODULE-LEVEL SHUTDOWN_IDLE TEST
# ============================================================================

@pytest.mark.asyncio
async def test_module_shutdown_idle_curator_workers():
    """shutdown_idle_curator_workers evicts stale workers."""
    try:
        await shutdown_all_curator_workers()

        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # Create workers
            for i in range(2):
                enqueue_curator_event(
                    session_id=f"test_idle_{i}",
                    turn_number=1,
                    user_message="msg",
                    session_goal=None,
                    messages=[],
                )

            registry = get_worker_registry()
            assert registry.active_count == 2

            # Wait for workers to become stale
            await asyncio.sleep(0.15)

            # Evict with ttl=0.1s
            evicted = await shutdown_idle_curator_workers(0.1)
            assert evicted >= 1
    finally:
        await shutdown_all_curator_workers()


# ============================================================================
# 14: RUN_PASS KWARGS CORRECTNESS TEST
# ============================================================================

@pytest.mark.asyncio
async def test_run_pass_calls_with_correct_kwargs():
    """_run_pass calls run_background_pass with all event fields as kwargs."""
    worker = CuratorWorker("test_session", debounce_ms=10, max_queue=100)
    worker.start()

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock) as mock_pass:
            event = SessionEvent(
                session_id="sess_1",
                turn_number=5,
                user_message="hello world",
                session_goal="find bugs",
                messages=[{"role": "user", "content": "test"}],
            )
            worker.enqueue(event)

            await asyncio.sleep(0.15)

            # Verify the call
            assert mock_pass.await_count == 1
            call_kwargs = mock_pass.call_args[1]
            assert call_kwargs["session_id"] == "sess_1"
            assert call_kwargs["turn_number"] == 5
            assert call_kwargs["user_message"] == "hello world"
            assert call_kwargs["session_goal"] == "find bugs"
            assert call_kwargs["messages"] == [{"role": "user", "content": "test"}]
    finally:
        await worker.aclose()


# ============================================================================
# 15: MULTIPLE_SEQUENTIAL_PASSES TEST
# ============================================================================

@pytest.mark.asyncio
async def test_multiple_sequential_passes():
    """Enqueuing events after debounce window allows sequential passes."""
    worker = CuratorWorker("test_session", debounce_ms=20, max_queue=100)
    worker.start()

    try:
        with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
            # First event
            event1 = SessionEvent(session_id="test_session", turn_number=1, user_message="msg1")
            worker.enqueue(event1)
            await asyncio.sleep(0.15)

            assert worker.passes_run == 1

            # Second event (after debounce window, so new pass)
            event2 = SessionEvent(session_id="test_session", turn_number=2, user_message="msg2")
            worker.enqueue(event2)
            await asyncio.sleep(0.15)

            assert worker.passes_run == 2
    finally:
        await worker.aclose()


# ============================================================================
# Single-leader leasing (archolith-maintenance SchedulerLeaseStore)
# ============================================================================

@pytest.mark.asyncio
async def test_no_lease_store_is_always_leader():
    """Without a lease store the registry is always leader (behavior unchanged)."""
    with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
        reg = WorkerRegistry(debounce_ms=5, lease_store=None)
        try:
            reg.enqueue(SessionEvent(session_id="s1", turn_number=1, user_message="m"))
            assert reg.active_count == 1
        finally:
            await reg.shutdown_all()


@pytest.mark.asyncio
async def test_lease_blocks_non_leader_registry(tmp_path):
    """A second registry sharing the lease DB is blocked while the first holds it."""
    from archolith_maintenance import SchedulerLeaseStore

    store_a = SchedulerLeaseStore(db_path=tmp_path / "leases.db")
    store_b = SchedulerLeaseStore(db_path=tmp_path / "leases.db")
    with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
        leader = WorkerRegistry(debounce_ms=5, lease_store=store_a, lease_duration_s=60)
        follower = WorkerRegistry(debounce_ms=5, lease_store=store_b, lease_duration_s=60)
        try:
            leader.enqueue(SessionEvent(session_id="s1", turn_number=1, user_message="m"))
            assert leader.active_count == 1  # leader runs workers

            follower.enqueue(SessionEvent(session_id="s1", turn_number=1, user_message="m"))
            assert follower.active_count == 0  # blocked — not the leader
        finally:
            await leader.shutdown_all()
            await follower.shutdown_all()


@pytest.mark.asyncio
async def test_shutdown_all_releases_lease_for_takeover(tmp_path):
    """After the leader releases on shutdown, a second registry can take the lease."""
    from archolith_maintenance import SchedulerLeaseStore

    store_a = SchedulerLeaseStore(db_path=tmp_path / "leases.db")
    store_b = SchedulerLeaseStore(db_path=tmp_path / "leases.db")
    with patch("archolith_proxy.curator.pipeline.run_background_pass", new_callable=AsyncMock):
        leader = WorkerRegistry(debounce_ms=5, lease_store=store_a, lease_duration_s=60)
        leader.enqueue(SessionEvent(session_id="s1", turn_number=1, user_message="m"))
        assert leader.active_count == 1
        await leader.shutdown_all()  # releases the lease

        follower = WorkerRegistry(debounce_ms=5, lease_store=store_b, lease_duration_s=60)
        try:
            follower.enqueue(SessionEvent(session_id="s1", turn_number=1, user_message="m"))
            assert follower.active_count == 1  # took over leadership
        finally:
            await follower.shutdown_all()


__all__ = [
    "test_debounce_coalesces_burst_to_latest",
    "test_in_flight_pass_not_cancelled",
    "test_idle_gate_no_pass_without_events",
    "test_session_end_stops_drain_loop",
    "test_registry_lazy_creates_workers",
    "test_registry_shutdown_idle",
    "test_registry_shutdown_session",
    "test_registry_shutdown_all",
    "test_backpressure_enqueue_never_blocks",
    "test_events_coalesced_counter",
    "test_last_active_tracking",
    "test_module_singleton_enqueue",
    "test_module_shutdown_idle_curator_workers",
    "test_run_pass_calls_with_correct_kwargs",
    "test_multiple_sequential_passes",
]
