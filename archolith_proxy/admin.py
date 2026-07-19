"""Admin token verification dependency for operator surfaces.

When ADMIN_TOKEN is empty (default), requests are allowed only from loopback
peers (localhost-only assumption); non-loopback peers are rejected unless
ADMIN_ALLOW_OPEN_NONLOCAL is set. When ADMIN_TOKEN is set, operator endpoints
require either X-Admin-Token or Authorization: Bearer matching the value.
"""

from __future__ import annotations

import hmac
import ipaddress

from fastapi import HTTPException, Request

from archolith_proxy.config import get_settings


def _is_loopback(host: str | None) -> bool:
    """Return True only for parseable loopback addresses (127.0.0.0/8, ::1).

    Unknown/unparseable hosts (including None and hostnames like 'localhost')
    are treated as non-loopback — fail closed.
    """
    if not host:
        return False
    # Normalize IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) to the embedded IPv4.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_loopback


async def require_admin_token(request: Request) -> None:
    """FastAPI dependency that validates admin token on operator surfaces.

    When ADMIN_TOKEN is empty, allow only loopback peers (unless
    ADMIN_ALLOW_OPEN_NONLOCAL is set). When set, check X-Admin-Token header or
    Authorization: Bearer <token>.
    """
    settings = get_settings()
    if not settings.admin_token:
        if settings.admin_allow_open_nonlocal:
            return  # Operator explicitly opted into open access
        client_host = request.client.host if request.client else None
        if _is_loopback(client_host):
            return  # No token configured — open for loopback only
        raise HTTPException(
            status_code=401,
            detail=(
                "Admin API requires ADMIN_TOKEN when accessed from a "
                "non-loopback address. Set ADMIN_TOKEN, or "
                "ADMIN_ALLOW_OPEN_NONLOCAL=true to allow open access."
            ),
        )

    # Check X-Admin-Token header first
    admin_header = request.headers.get("x-admin-token")
    if admin_header is not None and hmac.compare_digest(admin_header, settings.admin_token):
        return

    # Check Authorization: Bearer <token>
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if hmac.compare_digest(token, settings.admin_token):
            return

    raise HTTPException(
        status_code=401,
        detail="Invalid or missing admin token. Use X-Admin-Token header or Authorization: Bearer <token>.",
    )
