"""/v1/models endpoint — proxies upstream model list."""

import httpx
from fastapi import APIRouter, Request
from starlette.responses import Response

from src.config import get_settings
from src.openai.errors import make_error_response

router = APIRouter()


@router.get("/models")
async def list_models(request: Request) -> Response:
    """Proxy GET /v1/models to upstream API."""
    settings = get_settings()
    try:
        resp = await request.app.state.http_client.get(
            f"{settings.upstream_api_url}/models",
            headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
        )
    except httpx.ConnectError as e:
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")


@router.get("/models/{model_id:path}")
async def get_model(request: Request, model_id: str) -> Response:
    """Proxy GET /v1/models/{model_id} to upstream API."""
    settings = get_settings()
    try:
        resp = await request.app.state.http_client.get(
            f"{settings.upstream_api_url}/models/{model_id}",
            headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
        )
    except httpx.ConnectError as e:
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
