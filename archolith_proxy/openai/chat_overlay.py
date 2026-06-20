"""Per-session settings overlay for chat completions."""

from __future__ import annotations

import json

import structlog

from archolith_proxy.config import (
    SESSION_CONFIG_DENYLIST,
    build_effective_settings,
    set_session_settings,
)
from archolith_proxy.graph.backend import get_backend

logger = structlog.get_logger()


async def _clear_session_overlay():
    """Request-scoped dependency clearing the task-local settings overlay."""
    try:
        yield
    finally:
        set_session_settings(None)


async def _apply_session_config_overlay(header_value: str | None, session_id: str, settings, backend_factory=get_backend):
    """Merge X-Session-Config into persisted session overrides and activate it."""
    backend = backend_factory()

    if header_value:
        incoming = None
        try:
            parsed = json.loads(header_value)
            if not isinstance(parsed, dict):
                raise ValueError("X-Session-Config must be a JSON object")
            incoming = parsed
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("session_config_header_invalid", session_id=session_id, error=str(e))

        if incoming is not None:
            denied = sorted(k for k in incoming if k in SESSION_CONFIG_DENYLIST)
            unknown = sorted(k for k in incoming if k not in SESSION_CONFIG_DENYLIST and not hasattr(settings, k))
            if denied:
                logger.warning("session_config_denied_fields", session_id=session_id, fields=denied)
            if unknown:
                logger.warning("session_config_unknown_fields", session_id=session_id, fields=unknown)

            existing_json = await backend.get_session_config_overrides(session_id)
            try:
                merged = json.loads(existing_json) if existing_json else {}
                if not isinstance(merged, dict):
                    merged = {}
            except (ValueError, json.JSONDecodeError):
                merged = {}
            applied = {
                k: v for k, v in incoming.items()
                if k not in SESSION_CONFIG_DENYLIST and hasattr(settings, k)
            }
            if applied:
                merged.update(applied)
                await backend.set_session_config_overrides(session_id, json.dumps(merged))
                logger.info("session_config_applied", session_id=session_id, fields=sorted(applied))

    overrides_json = await backend.get_session_config_overrides(session_id)
    if not overrides_json:
        return settings
    try:
        overrides = json.loads(overrides_json)
    except (ValueError, json.JSONDecodeError):
        logger.warning("session_config_load_corrupt", session_id=session_id)
        return settings
    if not isinstance(overrides, dict) or not overrides:
        return settings

    effective = build_effective_settings(overrides)
    set_session_settings(effective)
    return effective
