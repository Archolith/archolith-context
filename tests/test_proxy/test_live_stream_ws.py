"""Integration tests for the live-stream WebSocket route."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from archolith_proxy.proxy.live import EVT_REQUEST, LiveStream
from archolith_proxy.routers.live_router import router as live_router


def _settings() -> SimpleNamespace:
    return SimpleNamespace(admin_token="secret", ws_allow_anonymous=False)


def _app_with_live_stream(live_stream) -> FastAPI:
    app = FastAPI()
    app.include_router(live_router)
    app.state.live_stream = live_stream

    @app.post("/test/publish")
    async def publish() -> dict[str, bool]:
        await live_stream.broadcast(EVT_REQUEST, {"session_id": "s1", "turn": 1})
        return {"published": True}

    return app


class OverflowSentinelStream:
    """Small test stream that deterministically emits the LiveStream overflow sentinel."""

    def __init__(self, max_queue: int = 4) -> None:
        self._max_queue = max_queue
        self._subscribers: list[asyncio.Queue] = []
        self._published = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def broadcast(self, event_type: str, data: dict) -> None:
        self._published += 1
        event = {"type": event_type, **data}
        for q in list(self._subscribers):
            if self._published > self._max_queue:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "dropped", "reason": "queue_overflow"})
                continue
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "dropped", "reason": "queue_overflow"})


def test_ws_subscribes_and_receives_events() -> None:
    live_stream = LiveStream()
    app = _app_with_live_stream(live_stream)

    with patch("archolith_proxy.routers.live_router.get_settings", return_value=_settings()):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/stream?token=secret") as websocket:
                response = client.post("/test/publish")
                assert response.status_code == 200

                event = websocket.receive_json()

    assert event["type"] == EVT_REQUEST
    assert event["session_id"] == "s1"
    assert event["turn"] == 1
    assert "ts" in event


def test_ws_closes_on_queue_overflow() -> None:
    live_stream = OverflowSentinelStream(max_queue=4)
    app = _app_with_live_stream(live_stream)

    with patch("archolith_proxy.routers.live_router.get_settings", return_value=_settings()):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/stream?token=secret") as websocket:
                for _ in range(5):
                    response = client.post("/test/publish")
                    assert response.status_code == 200

                event = None
                for _ in range(6):
                    candidate = websocket.receive_json()
                    if candidate.get("type") == "dropped":
                        event = candidate
                        break

                assert event == {"type": "dropped", "reason": "queue_overflow"}

                with pytest.raises(WebSocketDisconnect) as exc:
                    websocket.receive_json()

    assert exc.value.code == 1008
