"""Catch-all passthrough for unrecognized /v1/* routes."""

from fastapi import APIRouter, Request
from starlette.responses import Response

from src.config import get_settings

router = APIRouter()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def passthrough(request: Request, path: str) -> Response:
    """Relay any unrecognized /v1/* request to upstream unchanged."""
    settings = get_settings()
    url = f"{settings.upstream_api_url}/{path}"

    # Filter headers — remove hop-by-hop and host
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    headers["Authorization"] = f"Bearer {settings.upstream_api_key}"

    body = await request.body()

    async with request.app.state.http_client.stream(
        request.method,
        url,
        headers=headers,
        content=body,
    ) as resp:
        content = await resp.aread()
        return Response(
            content=content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )
