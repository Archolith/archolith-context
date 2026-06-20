"""Admin configuration and shutdown endpoints."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from archolith_proxy.admin import require_admin_token
from archolith_proxy.config import _write_overrides, get_settings, get_settings_delta
from archolith_proxy.graph.backend import get_backend, is_graph_ready
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()

router = APIRouter()

try:
    from archolith_compliance.retention import BackendDeletionResult, delete_session
except ImportError:  # pragma: no cover - fallback for installs without the optional compliance extra
    @dataclass(frozen=True)
    class BackendDeletionResult:
        backend: str
        deleted: bool
        detail: str = ""
        error: str | None = None

    @dataclass(frozen=True)
    class _DeletionReport:
        session_id: str
        results: tuple[BackendDeletionResult, ...]

        @property
        def succeeded(self) -> bool:
            return all(result.deleted for result in self.results)

        @property
        def deleted_backends(self) -> tuple[str, ...]:
            return tuple(result.backend for result in self.results if result.deleted)

        @property
        def failed_backends(self) -> tuple[str, ...]:
            return tuple(result.backend for result in self.results if not result.deleted)

    def delete_session(session_id: str, *, backends) -> _DeletionReport:
        results = []
        for backend in backends:
            backend_name = getattr(backend, "backend_name", backend.__class__.__name__)
            try:
                raw = backend.delete_session_data(session_id)
                if isinstance(raw, BackendDeletionResult):
                    results.append(raw)
                elif isinstance(raw, bool):
                    results.append(BackendDeletionResult(backend=backend_name, deleted=raw))
                else:
                    results.append(BackendDeletionResult(backend=backend_name, deleted=True, detail=str(raw)))
            except Exception as exc:
                results.append(BackendDeletionResult(backend=backend_name, deleted=False, error=str(exc)))
        return _DeletionReport(session_id=session_id, results=tuple(results))


@dataclass(frozen=True)
class _PrecomputedDeletionBackend:
    backend_name: str
    result: object

    def delete_session_data(self, session_id: str) -> object:
        return self.result

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
        if key == "synthetic_tools_enabled":
            raise HTTPException(
                status_code=422,
                detail=(
                    "synthetic_tools_enabled is denylisted from runtime admin config; "
                    "set SYNTHETIC_TOOLS_ENABLED in the environment for the deprecated escape hatch"
                ),
            )
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


@router.get("/admin/sessions/{session_id}/stored")
async def get_session_stored(
    session_id: str,
    admin: None = Depends(require_admin_token),
) -> dict:
    """Enumerate stores that currently hold data for one session."""
    graph = await _graph_storage_summary(session_id)
    trace = await get_trace_store().session_storage_summary(session_id)
    return {
        "session_id": session_id,
        "stores": {
            "graph": graph,
            "trace": trace,
        },
        "present": bool(graph.get("present") or trace.get("present")),
    }


@router.delete("/admin/sessions/{session_id}")
async def delete_session_data(
    session_id: str,
    admin: None = Depends(require_admin_token),
) -> dict:
    """Delete all known data for one session."""
    graph_result = await _delete_graph_session_data(session_id, BackendDeletionResult)
    trace_detail = await get_trace_store().delete_session_data(session_id)
    trace_result = BackendDeletionResult(
        backend="trace",
        deleted=True,
        detail=str(trace_detail),
    )

    report = delete_session(
        session_id,
        backends=[
            _PrecomputedDeletionBackend("graph", graph_result),
            _PrecomputedDeletionBackend("trace", trace_result),
        ],
    )
    return {
        "session_id": report.session_id,
        "succeeded": report.succeeded,
        "deleted_backends": list(report.deleted_backends),
        "failed_backends": list(report.failed_backends),
        "results": [
            {
                "backend": result.backend,
                "deleted": result.deleted,
                "detail": result.detail,
                "error": result.error,
            }
            for result in report.results
        ],
    }


async def _graph_storage_summary(session_id: str) -> dict:
    summary = {
        "configured": is_graph_ready(),
        "present": False,
        "session": False,
        "active_facts": 0,
        "cached_files": 0,
    }
    if not is_graph_ready():
        return summary

    backend = get_backend()
    try:
        summary["session"] = bool(await backend.find_session_by_id(session_id))
        summary["active_facts"] = await backend.get_active_fact_count(session_id)
    except Exception as exc:
        summary["error"] = str(exc)

    try:
        summary["cached_files"] = len(await backend.list_cached_files(session_id))
    except Exception:
        summary["cached_files"] = 0

    summary["present"] = bool(summary["session"] or summary["active_facts"] or summary["cached_files"])
    return summary


async def _delete_graph_session_data(session_id: str, result_type):
    if not is_graph_ready():
        return result_type(backend="graph", deleted=False, detail="graph backend not configured")
    try:
        detail = await get_backend().delete_session_data(session_id)
        return result_type(backend="graph", deleted=True, detail=str(detail))
    except Exception as exc:
        return result_type(backend="graph", deleted=False, error=str(exc))


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
