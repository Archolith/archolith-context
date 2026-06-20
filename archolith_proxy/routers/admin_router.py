"""Admin configuration and shutdown endpoints."""

from __future__ import annotations

import os
import signal

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from archolith_proxy.admin import require_admin_token
from archolith_proxy.config import _write_overrides, get_settings, get_settings_delta

logger = structlog.get_logger()

router = APIRouter()

TUNABLE_FIELDS = {
    "context_token_budget",
    "max_rewritten_tokens",
    "coherence_tail_size",
    "max_tail_messages",
    "cold_start_turns",
    "cold_start_token_threshold",
    "assembly_min_savings_ratio",
    "assembly_min_input_tokens",
    "assembly_latency_budget_ms",
    "session_ttl_hours",
    "embedding_enabled",
    "compaction_enabled",
    "query_rewrite_enabled",
    "session_recall_tool_enabled",
    "filter_enabled",
    "pricing_input_per_million",
    "pricing_input_cached_per_million",
    "pricing_output_per_million",
    "agent_solo_shrink_enabled",
    "agent_solo_dedup_enabled",
    "agent_solo_compress_middle_enabled",
    "agent_solo_shrink_max_tokens",
    "agent_solo_min_input_tokens",
    "agent_solo_dump_payloads",
    "curator_enabled",
    "curator_max_iterations",
    "curator_latency_budget_ms",
    "curation_mode",
    "prepper_model",
    "prepper_max_iterations",
    "prepper_debounce_ms",
    "prepper_latency_budget_ms",
    "assembler_model",
    "assembler_max_iterations",
    "assembler_latency_budget_ms",
    "drop_middle_on_assembly",
}


@router.get("/admin/config")
async def get_config(admin: None = Depends(require_admin_token)) -> dict:
    """Return current runtime-tunable configuration."""
    settings = get_settings()
    return {k: getattr(settings, k) for k in sorted(TUNABLE_FIELDS)}


@router.patch("/admin/config")
@router.post("/admin/config")
async def update_config(
    request: Request,
    admin: None = Depends(require_admin_token),
    persist: bool = Query(
        True,
        description="When false, apply in-memory only and do NOT write "
        "config_overrides.json (changes evaporate on restart). Use for "
        "benchmarks/experiments that must not mutate persisted config.",
    ),
) -> dict:
    """Update runtime-tunable configuration fields.

    Accepts a JSON object with one or more tunable fields. Changes take
    effect immediately for subsequent requests (no restart needed).

    By default, overrides are persisted to config_overrides.json and re-applied
    on the next startup. Pass ?persist=false to apply in-memory only (e.g. a
    benchmark arm) so the proxy's persisted config is never mutated.
    """
    body = await request.json()
    settings = get_settings()
    updated = {}
    rejected = {}
    for key, value in body.items():
        if key not in TUNABLE_FIELDS:
            rejected[key] = "not a tunable field"
            continue
        expected_type = type(getattr(settings, key))
        try:
            # Special handling for bool fields: parse string booleans properly
            if expected_type is bool:
                if isinstance(value, str):
                    if value.lower() in ("true", "1", "yes"):
                        coerced = True
                    elif value.lower() in ("false", "0", "no"):
                        coerced = False
                    else:
                        raise ValueError(f"Cannot parse '{value}' as boolean")
                else:
                    coerced = bool(value)
            else:
                coerced = expected_type(value)
            setattr(settings, key, coerced)
            updated[key] = coerced
            logger.info("config_updated", field=key, value=coerced)
        except (ValueError, TypeError) as e:
            rejected[key] = f"invalid value: {e}"
    if updated and persist:
        _write_overrides(updated)
    if persist:
        warning = ("Changes persist across restarts via config_overrides.json. "
                   "Delete that file and restart to reset to env defaults.")
    else:
        warning = ("Applied in-memory only (persist=false). Changes are NOT written "
                   "to config_overrides.json and evaporate on the next restart.")
    return {
        "updated": updated,
        "rejected": rejected,
        "persisted": bool(persist),
        "warning": warning,
    }


@router.get("/admin/config-delta")
async def get_config_delta(admin: None = Depends(require_admin_token)) -> dict:
    """Return the delta between base env/settings values and current overrides."""
    return get_settings_delta()


@router.post("/admin/shutdown")
async def graceful_shutdown(
    background_tasks: BackgroundTasks,
    admin: None = Depends(require_admin_token),
) -> dict:
    """Gracefully shut down the proxy.

    Sends SIGTERM to the current process, triggering the lifespan cleanup
    path (closes LadybugDB with WAL flush, drains HTTP clients, etc.).
    Use this instead of SIGKILL / Stop-Process -Force to avoid WAL corruption.

    Returns immediately; shutdown completes in the background.
    """
    pid = os.getpid()
    logger.info("graceful_shutdown_requested", pid=pid)

    async def _send_sigterm() -> None:
        import asyncio

        await asyncio.sleep(0.1)  # let the HTTP response flush first
        os.kill(pid, signal.SIGTERM)

    background_tasks.add_task(_send_sigterm)
    return {
        "ok": True,
        "pid": pid,
        "note": "SIGTERM sent — proxy will shut down after current requests complete",
    }
