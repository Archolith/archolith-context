"""Admin token verification dependency for operator surfaces.

When ADMIN_TOKEN is empty (default), all requests are allowed (localhost-only
assumption per deployment plan). When set, operator endpoints require either
X-Admin-Token or Authorization: Bearer matching the configured value.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from archolith_proxy.config import get_settings


async def require_admin_token(request: Request) -> None:
    """FastAPI dependency that validates admin token on operator surfaces.

    Skip validation when ADMIN_TOKEN is empty (localhost-only deployment).
    When set, check X-Admin-Token header or Authorization: Bearer <token>.
    """
    settings = get_settings()
    if not settings.admin_token:
        return  # No token configured — open access (localhost assumption)

    # Check X-Admin-Token header first
    admin_header = request.headers.get("x-admin-token")
    if admin_header == settings.admin_token:
        return

    # Check Authorization: Bearer <token>
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == settings.admin_token:
            return

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing admin token. Use X-Admin-Token header or Authorization: Bearer <token>.",
    )
