"""Tests for the live stream broadcaster (src/proxy/live.py)."""

from __future__ import annotations

import asyncio

import pytest

from archolith_proxy.proxy.live import (
    EVT_ASSEMBLY,
    EVT_EXTRACTION,
    EVT_RECALL,
    EVT_REQUEST,
    EVT_RESPONSE,
    EVT_SESSION,
    LiveStream,
    broadcast_assembly,
    broadcast_extraction,
    broadcast_recall,
    broadcast_request,
    broadcast_response,
    broadcast_session_event,
    get_live_stream,
    reset_live_stream,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singleton before each test."""
    reset_live_stream()
    yield
    reset_live_stream()


# --- LiveStream core tests ---


class TestLiveStreamCore:
    """Tests for LiveStream broadcast/subscribe/unsubscribe."""

    @pytest.mark.asyncio
    async def test_broadcast_delivers_to_subscriber(self):
        ls = LiveStream()
        q = await ls.subscribe()
        await ls.broadcast(EVT_REQUEST, {"session_id": "s1", "turn": 1})
        event = q.get_nowait()
        assert event["type"] == EVT_REQUEST
        assert event["session_id"] == "s1"
        assert event["turn"] == 1
        assert "ts" in event  # timestamp

    @pytest.mark.asyncio
    async def test_broadcast_delivers_to_multiple_subscribers(self):
        ls = LiveStream()
        q1 = await ls.subscribe()
        q2 = await ls.subscribe()
        await ls.broadcast(EVT_RESPONSE, {"status": 200})
        assert q1.get_nowait()["type"] == EVT_RESPONSE
        assert q2.get_nowait()["type"] == EVT_RESPONSE

    @pytest.mark.asyncio
    async def test_subscriber_count(self):
        ls = LiveStream()
        assert ls.subscriber_count == 0
        q1 = await ls.subscribe()
        assert ls.subscriber_count == 1
        q2 = await ls.subscribe()
        assert ls.subscriber_count == 2
        await ls.unsubscribe(q1)
        assert ls.subscriber_count == 1
        await ls.unsubscribe(q2)
        assert ls.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_event_count(self):
        ls = LiveStream()
        assert ls.event_count == 0
        await ls.broadcast(EVT_REQUEST, {"a": 1})
        assert ls.event_count == 1
        await ls.broadcast(EVT_RESPONSE, {"b": 2})
        assert ls.event_count == 2

    @pytest.mark.asyncio
    async def test_unsubscribe_already_removed_is_safe(self):
        ls = LiveStream()
        q = await ls.subscribe()
        await ls.unsubscribe(q)
        # Second unsubscribe should not raise
        await ls.unsubscribe(q)

    @pytest.mark.asyncio
    async def test_broadcast_with_no_subscribers_is_safe(self):
        ls = LiveStream()
        # Should not raise
        await ls.broadcast(EVT_REQUEST, {"session_id": "s1"})

    @pytest.mark.asyncio
    async def test_slow_consumer_gets_dropped(self):
        ls = LiveStream()
        # Use a small queue — fill it completely, then overflow
        q = asyncio.Queue(maxsize=2)
        async with ls._lock:
            ls._subscribers.append(q)

        # Fill the queue to capacity
        await ls.broadcast(EVT_REQUEST, {"fill": 1})
        await ls.broadcast(EVT_REQUEST, {"fill": 2})
        assert ls.subscriber_count == 1

        # Overflow — subscriber removed, sentinel enqueued if room
        await ls.broadcast(EVT_REQUEST, {"overflow": 1})
        assert ls.subscriber_count == 0  # Dropped regardless of sentinel

        # Check that at least one event was delivered before drop
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert len(events) >= 2  # The two that fit

    @pytest.mark.asyncio
    async def test_slow_consumer_dropped_even_if_sentinel_fails(self):
        """Even if the sentinel can't be enqueued (queue full), subscriber is removed."""
        ls = LiveStream()
        q = asyncio.Queue(maxsize=1)
        async with ls._lock:
            ls._subscribers.append(q)

        # Fill and overflow
        await ls.broadcast(EVT_REQUEST, {"fill": 1})
        assert ls.subscriber_count == 1
        await ls.broadcast(EVT_REQUEST, {"overflow": 1})
        assert ls.subscriber_count == 0  # Removed regardless of sentinel

    @pytest.mark.asyncio
    async def test_fast_consumer_not_dropped(self):
        ls = LiveStream()
        q = await ls.subscribe()
        for i in range(10):
            await ls.broadcast(EVT_REQUEST, {"i": i})
            # Drain immediately
            event = q.get_nowait()
            assert event["i"] == i
        assert ls.subscriber_count == 1


# --- Singleton management ---


class TestSingleton:
    """Tests for get_live_stream / reset_live_stream."""

    def test_get_returns_instance(self):
        ls = get_live_stream()
        assert isinstance(ls, LiveStream)

    def test_get_returns_same_instance(self):
        ls1 = get_live_stream()
        ls2 = get_live_stream()
        assert ls1 is ls2

    def test_reset_clears_instance(self):
        ls1 = get_live_stream()
        reset_live_stream()
        ls2 = get_live_stream()
        assert ls1 is not ls2


# --- Convenience broadcast functions ---


class TestConvenienceBroadcasts:
    """Tests for the broadcast_* convenience functions."""

    @pytest.mark.asyncio
    async def test_broadcast_request(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        await broadcast_request(
            session_id="s1", turn_number=3, model="gpt-4",
            message_count=5, stream=True, input_tokens=1200,
        )
        event = q.get_nowait()
        assert event["type"] == EVT_REQUEST
        assert event["session_id"] == "s1"
        assert event["turn"] == 3
        assert event["model"] == "gpt-4"
        assert event["messages"] == 5
        assert event["stream"] is True
        assert event["input_tokens"] == 1200

    @pytest.mark.asyncio
    async def test_broadcast_assembly(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        await broadcast_assembly(
            session_id="s1", turn_number=2, mode="hybrid",
            facts_injected=5, token_savings=800, latency_ms=45.7,
        )
        event = q.get_nowait()
        assert event["type"] == EVT_ASSEMBLY
        assert event["mode"] == "hybrid"
        assert event["facts_injected"] == 5
        assert event["token_savings"] == 800
        assert event["latency_ms"] == 45.7

    @pytest.mark.asyncio
    async def test_broadcast_response(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        await broadcast_response(
            session_id="s1", turn_number=1, status=200,
            latency_ms=123.4, output_tokens=50,
        )
        event = q.get_nowait()
        assert event["type"] == EVT_RESPONSE
        assert event["status"] == 200
        assert event["output_tokens"] == 50

    @pytest.mark.asyncio
    async def test_broadcast_extraction(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        await broadcast_extraction(
            session_id="s1", turn_number=4, facts_stored=3,
            session_goal="Build a REST API", latency_ms=210.0,
        )
        event = q.get_nowait()
        assert event["type"] == EVT_EXTRACTION
        assert event["facts_stored"] == 3
        assert event["session_goal"] == "Build a REST API"

    @pytest.mark.asyncio
    async def test_broadcast_extraction_truncates_long_goal(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        long_goal = "x" * 200
        await broadcast_extraction(
            session_id="s1", turn_number=1, facts_stored=0,
            session_goal=long_goal,
        )
        event = q.get_nowait()
        assert len(event["session_goal"]) == 80

    @pytest.mark.asyncio
    async def test_broadcast_session_event(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        await broadcast_session_event("s1", "goal_set_initial", goal="My project")
        event = q.get_nowait()
        assert event["type"] == EVT_SESSION
        assert event["event"] == "goal_set_initial"
        assert event["goal"] == "My project"

    @pytest.mark.asyncio
    async def test_broadcast_recall(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        await broadcast_recall(
            session_id="s1", turn_number=5,
            question="What did we decide about auth?", facts_returned=7,
        )
        event = q.get_nowait()
        assert event["type"] == EVT_RECALL
        assert event["question"] == "What did we decide about auth?"
        assert event["facts_returned"] == 7

    @pytest.mark.asyncio
    async def test_broadcast_recall_truncates_long_question(self):
        ls = get_live_stream()
        q = await ls.subscribe()
        long_q = "q" * 500
        await broadcast_recall(session_id="s1", turn_number=1, question=long_q, facts_returned=0)
        event = q.get_nowait()
        assert len(event["question"]) == 200

    @pytest.mark.asyncio
    async def test_convenience_skips_serialization_when_zero_subscribers(self):
        """When no subscribers, convenience functions return early without calling broadcast."""
        ls = get_live_stream()
        assert ls.subscriber_count == 0
        # These should complete without error and without incrementing event_count
        await broadcast_request(session_id=None, turn_number=1, model="gpt-4",
                                message_count=1, stream=False, input_tokens=10)
        await broadcast_assembly(session_id=None, turn_number=1, mode="passthrough")
        await broadcast_response(session_id=None, turn_number=1, status=200, latency_ms=0)
        await broadcast_extraction(session_id="s1", turn_number=1, facts_stored=0)
        await broadcast_session_event("s1", "created")
        await broadcast_recall(session_id="s1", turn_number=1, question="q", facts_returned=0)
        assert ls.event_count == 0  # No events broadcast
