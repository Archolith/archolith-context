"""/v1/models endpoint — proxies upstream model list."""

from fastapi import APIRouter, Request

from src.config import get_settings

router = APIRouter()


@router.get("/models")
async def list_models(request: Request) -> dict:
    """Proxy GET /v1/models to upstream API."""
    settings = get_settings()
    async with request.app.state.http_client.stream(
        "GET",
        f"{settings.upstream_api_url}/models",
        headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
    ) as resp:
        return resp.json()


@router.get("/models/{model_id:path}")
async def get_model(request: Request, model_id: str) -> dict:
    """Proxy GET /v1/models/{model_id} to upstream API."""
    settings = get_settings()
    async with request.app.state.http_client.stream(
        "GET",
        f"{settings.upstream_api_url}/models/{model_id}",
        headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
    ) as resp:
        return resp.json()
