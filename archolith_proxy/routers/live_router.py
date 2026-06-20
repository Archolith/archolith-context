"""WebSocket live stream endpoint for real-time proxy events."""

from __future__ import annotations

import hmac

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from archolith_proxy.admin import _is_loopback
from archolith_proxy.config import get_settings

logger = structlog.get_logger()

router = APIRouter()


def _websocket_authorized(websocket: WebSocket, admin_token: str, ws_allow_anonymous: bool) -> bool:
    """Apply the admin boundary rules to the live-stream WebSocket handshake."""
    if ws_allow_anonymous:
        return True

    if not admin_token:
        client_host = websocket.client.host if websocket.client else None
        return _is_loopback(client_host)

    query_token = websocket.query_params.get("token", "")
    if query_token and hmac.compare_digest(query_token, admin_token):
        return True

    header_token = websocket.headers.get("x-admin-token", "")
    if header_token and hmac.compare_digest(header_token, admin_token):
        return True

    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return hmac.compare_digest(auth_header[7:], admin_token)

    return False


@router.websocket("/ws/stream")
async def ws_live_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time proxy event streaming.

    Clients connect and receive JSON events for every request, assembly,
    response, extraction, and recall event that flows through the proxy.
    Slow clients are disconnected after 256 queued events.

    Uses an explicit live-stream boundary: WS_ALLOW_ANONYMOUS=true preserves the
    legacy open feed; otherwise ADMIN_TOKEN is enforced when set, and empty-token
    local development is loopback-only.
    """
    settings = get_settings()
    if not _websocket_authorized(
        websocket,
        admin_token=settings.admin_token,
        ws_allow_anonymous=settings.ws_allow_anonymous,
    ):
        await websocket.close(
            code=4001,
            reason="non-loopback access requires ADMIN_TOKEN or ws_allow_anonymous=True",
        )
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
