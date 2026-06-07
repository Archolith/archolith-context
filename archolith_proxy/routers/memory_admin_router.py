"""Memory engine and promotion admin endpoints."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from archolith_proxy.admin import require_admin_token
from archolith_proxy.config import get_settings

logger = structlog.get_logger()

router = APIRouter()


@router.get("/memory-engines")
async def list_memory_engines(
    admin: None = Depends(require_admin_token),
) -> dict:
    """List all configured memory engines and their health."""
    from archolith_proxy.memory.registry import get_registry

    registry = get_registry()
    engines = registry.list_engines()
    health = await registry.healthcheck_all()
    return {
        "engines": engines,
        "health": health,
        "default_engine_id": registry.default_engine_id,
        "promotion_enabled": get_settings().promotion_enabled,
    }


@router.get("/memory-engines/{engine_id}")
async def get_memory_engine(
    engine_id: str, admin: None = Depends(require_admin_token)
) -> dict:
    """Get details and health for a specific memory engine."""
    from archolith_proxy.memory.registry import get_registry

    registry = get_registry()
    config = registry.get_config(engine_id)
    if config is None:
        return JSONResponse(
            status_code=404, content={"error": f"Engine {engine_id} not found"}
        )
    adapter = registry.get_adapter(engine_id)
    caps = await adapter.capabilities() if adapter else None
    healthy = False
    if adapter:
        try:
            healthy = await adapter.healthcheck()
        except Exception:
            pass
    return {
        "id": config.id,
        "type": config.type,
        "enabled": config.enabled,
        "priority": config.priority,
        "base_url": config.base_url,
        "is_default": config.id == registry.default_engine_id,
        "healthy": healthy,
        "capabilities": caps.model_dump() if caps else None,
    }


@router.get("/promotions")
async def list_promotions(
    request: Request, admin: None = Depends(require_admin_token)
) -> dict:
    """List promotion history and stats."""
    svc = getattr(request.app.state, "promotion_service", None)
    if svc is None:
        return JSONResponse(
            status_code=503, content={"error": "Promotion service not initialized"}
        )
    return {
        "stats": svc.stats,
        "recent": [r.model_dump(mode="json") for r in svc.audit_trail[-50:]],
    }


@router.post("/promotions/retry/{promotion_id}")
async def retry_promotion(
    promotion_id: str,
    request: Request,
    admin: None = Depends(require_admin_token),
) -> dict:
    """Retry a failed promotion by its promotion_id (finds it in audit trail)."""
    svc = getattr(request.app.state, "promotion_service", None)
    if svc is None:
        return JSONResponse(
            status_code=503, content={"error": "Promotion service not initialized"}
        )
    original = None
    for r in svc.audit_trail:
        if r.promotion_id == promotion_id:
            original = r
            break
    if original is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"Promotion {promotion_id} not found"},
        )
    return JSONResponse(
        status_code=501,
        content={
            "error": "Not Implemented",
            "note": "Retry requires resubmission with the original PromotionRecord"
        },
    )
