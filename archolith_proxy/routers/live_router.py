"""WebSocket live stream endpoint for real-time proxy events."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from archolith_proxy.config import get_settings

logger = structlog.get_logger()

router = APIRouter()


@router.websocket("/ws/stream")
async def ws_live_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time proxy event streaming.

    Clients connect and receive JSON events for every request, assembly,
    response, extraction, and recall event that flows through the proxy.
    Slow clients are disconnected after 256 queued events.

    When ADMIN_TOKEN is set, clients must provide it via query param
    ?token=<value> or the connection is closed.
    """
    settings = get_settings()
    if settings.admin_token:
        token = websocket.query_params.get("token", "")
        if token != settings.admin_token:
            await websocket.close(code=4001, reason="Invalid admin token")
            return

    await websocket.accept()
    live_stream = getattr(websocket.app.state, "live_stream", None)
    if not live_stream:
        await websocket.close(code=1011, reason="Live stream not initialized")
        return

    q = await live_stream.subscribe()
    logger.info("live_stream_client_connected", subscribers=live_stream.subscriber_count)

    try:
        while True:
            event = await q.get()
            if event.get("type") == "dropped":
                await websocket.send_json(event)
                await websocket.close(code=1008, reason="Queue overflow - too slow")
                break
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("live_stream_client_error", error=str(e))
    finally:
        await live_stream.unsubscribe(q)
        logger.info("live_stream_client_disconnected", subscribers=live_stream.subscriber_count)
