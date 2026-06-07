"""Router packages — FastAPI endpoint modules for admin and operational surfaces."""

from archolith_proxy.routers.admin_router import router as admin_router
from archolith_proxy.routers.live_router import router as live_router
from archolith_proxy.routers.memory_admin_router import router as memory_admin_router
from archolith_proxy.routers.metrics_router import router as metrics_router
from archolith_proxy.routers.sessions_router import router as sessions_router

__all__ = [
    "admin_router",
    "live_router",
    "memory_admin_router",
    "metrics_router",
    "sessions_router",
]
