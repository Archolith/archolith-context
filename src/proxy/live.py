"""Live stream broadcaster for real-time proxy inspection.

Provides a WebSocket endpoint that broadcasts proxy activity events:
- incoming requests (model, message count, stream flag)
- context assembly results (mode, facts injected, token savings)
- upstream responses (status, latency, token usage)
- extraction results (facts stored, session goal updates)

Architecture: asyncio.PubSub pattern with per-client queues.
- Publishers call broadcast() with typed event dicts
- Each WebSocket client gets its own asyncio.Queue
- Slow clients are dropped (queue overflow) to prevent memory leaks
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

# Maximum events buffered per client before disconnect
_MAX_CLIENT_QUEUE_SIZE = 256

# Event types
EVT_REQUEST = "request"
EVT_ASSEMBLY = "assembly"
EVT_RESPONSE = "response"
EVT_EXTRACTION = "extraction"
EVT_SESSION = "session"
EVT_RECALL = "recall"


@dataclass
class LiveStream:
    """Process-level pub/sub hub for live proxy events.

    A single instance lives on app.state.live_stream. Publishers call
    broadcast() from any coroutine; subscribers consume via subscribe().
    """

    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _event_count: int = 0

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Publish an event to all connected subscribers.

        Drops subscribers whose queues are full (slow consumers).
        Non-blocking: never waits for a consumer.
        """
        event = {
            "type": event_type,
            "ts": time.time(),
            **data,
        }
        self._event_count += 1

        dead: list[asyncio.Queue] = []
        async with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)

            # Remove slow consumers
            for q in dead:
                self._subscribers.remove(q)
                # Put a sentinel so the subscriber knows it was dropped
                try:
                    q.put_nowait({"type": "dropped", "reason": "queue_overflow"})
                except asyncio.QueueFull:
                    pass

        if dead:
            logger.debug("live_stream_dropped_subscribers", count=len(dead))

    async def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber. Returns a queue to consume events from."""
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_CLIENT_QUEUE_SIZE)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        async with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass  # Already removed (overflow drop)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def event_count(self) -> int:
        return self._event_count


# Module-level singleton — created once in lifespan
_instance: LiveStream | None = None


def get_live_stream() -> LiveStream:
    """Get the process-level LiveStream instance."""
    global _instance
    if _instance is None:
        _instance = LiveStream()
    return _instance


def reset_live_stream() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None


# --- Convenience broadcast functions (called from chat.py) ---

async def broadcast_request(
    session_id: str | None,
    turn_number: int,
    model: str,
    message_count: int,
    stream: bool,
    input_tokens: int,
) -> None:
    """Broadcast an incoming request event."""
    ls = get_live_stream()
    if ls.subscriber_count == 0:
        return  # Skip serialization work if nobody is listening
    await ls.broadcast(EVT_REQUEST, {
        "session_id": session_id,
        "turn": turn_number,
        "model": model,
        "messages": message_count,
        "stream": stream,
        "input_tokens": input_tokens,
    })


async def broadcast_assembly(
    session_id: str | None,
    turn_number: int,
    mode: str,
    facts_injected: int = 0,
    token_savings: int = 0,
    latency_ms: float = 0.0,
) -> None:
    """Broadcast a context assembly event."""
    ls = get_live_stream()
    if ls.subscriber_count == 0:
        return
    await ls.broadcast(EVT_ASSEMBLY, {
        "session_id": session_id,
        "turn": turn_number,
        "mode": mode,
        "facts_injected": facts_injected,
        "token_savings": token_savings,
        "latency_ms": round(latency_ms, 1),
    })


async def broadcast_response(
    session_id: str | None,
    turn_number: int,
    status: int,
    latency_ms: float,
    output_tokens: int | None = None,
) -> None:
    """Broadcast an upstream response event."""
    ls = get_live_stream()
    if ls.subscriber_count == 0:
        return
    await ls.broadcast(EVT_RESPONSE, {
        "session_id": session_id,
        "turn": turn_number,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "output_tokens": output_tokens,
    })


async def broadcast_extraction(
    session_id: str,
    turn_number: int,
    facts_stored: int,
    session_goal: str | None = None,
    latency_ms: float = 0.0,
) -> None:
    """Broadcast an extraction result event."""
    ls = get_live_stream()
    if ls.subscriber_count == 0:
        return
    await ls.broadcast(EVT_EXTRACTION, {
        "session_id": session_id,
        "turn": turn_number,
        "facts_stored": facts_stored,
        "session_goal": session_goal[:80] if session_goal else None,
        "latency_ms": round(latency_ms, 1),
    })


async def broadcast_session_event(
    session_id: str,
    event: str,
    goal: str | None = None,
) -> None:
    """Broadcast a session lifecycle event (created, goal_set, etc.)."""
    ls = get_live_stream()
    if ls.subscriber_count == 0:
        return
    await ls.broadcast(EVT_SESSION, {
        "session_id": session_id,
        "event": event,
        "goal": goal[:80] if goal else None,
    })


async def broadcast_recall(
    session_id: str,
    turn_number: int,
    question: str,
    facts_returned: int,
) -> None:
    """Broadcast a session recall tool call event."""
    ls = get_live_stream()
    if ls.subscriber_count == 0:
        return
    await ls.broadcast(EVT_RECALL, {
        "session_id": session_id,
        "turn": turn_number,
        "question": question[:200],
        "facts_returned": facts_returned,
    })
