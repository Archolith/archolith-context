"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from archolith_proxy import __version__
from archolith_proxy.admin import require_admin_token
from archolith_proxy.config import get_settings
from archolith_proxy.graph.backend import close_backend, get_backend, init_backend, is_graph_ready
from archolith_proxy.graph.neo4j_backend import Neo4jBackend
from archolith_proxy.logging_config import configure_logging
from archolith_proxy.metrics import get_metrics, record_metric, record_start_time
from archolith_proxy.openai.router import router as openai_router
from archolith_proxy.routers.admin_router import router as admin_router
from archolith_proxy.routers.live_router import router as live_router
from archolith_proxy.routers.memory_admin_router import router as memory_admin_router
from archolith_proxy.routers.metrics_router import router as metrics_router
from archolith_proxy.routers.plugins import router as plugins_router
from archolith_proxy.routers.sessions_router import router as sessions_router
from archolith_proxy.trace.router import router as trace_router
from archolith_proxy.trace.store import get_trace_store

# Configure structured JSON logging before first use
configure_logging()

logger = structlog.get_logger()


def _load_memory_engines(settings, registry) -> None:
    """Load memory engine configs from env and register them."""
    import json

    from archolith_proxy.memory.models import MemoryEngineConfig

    if settings.memory_engines_json:
        try:
            engines_raw = json.loads(settings.memory_engines_json)
            if isinstance(engines_raw, list):
                for raw in engines_raw:
                    try:
                        cfg = MemoryEngineConfig(**raw)
                        registry.register(cfg)
                    except Exception as e:
                        logger.warning("memory_engine_config_invalid", raw=raw, error=str(e))
            logger.info("memory_engines_loaded", count=registry.engine_count)
        except json.JSONDecodeError as e:
            logger.warning("memory_engines_json_parse_error", error=str(e))
    elif settings.memory_api_url:
        registry.register(
            MemoryEngineConfig(
                id="archolith-memory",
                type="archolith_memory",
                enabled=True,
                priority=10,
                base_url=settings.memory_api_url,
                api_key_env="MEMORY_API_KEY",
            )
        )
        logger.info("memory_engine_auto_registered", engine_id="archolith-memory", base_url=settings.memory_api_url)


async def _init_neo4j_with_retry(settings, max_retries: int = 3, backoff_base: float = 1.0) -> bool:
    """Initialize Neo4j backend with retry logic. Returns True if connected."""
    for attempt in range(max_retries):
        try:
            await init_backend(Neo4jBackend())
            logger.info("neo4j_initialized", attempt=attempt + 1)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                delay = backoff_base * (2**attempt)
                logger.warning(
                    "neo4j_init_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay_s=delay,
                    error=str(e),
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "neo4j_init_failed",
                    attempts=max_retries,
                    error=str(e),
                    note="proxy will run without graph features",
                )
                return False
    return False


async def _background_cleanup_loop() -> None:
    """Hourly background task: expire sessions, delete expired, prune locks,
    evict stale curator and circuit-breaker caches."""
    while True:
        try:
            await asyncio.sleep(3600)
            if is_graph_ready():
                backend = get_backend()
                expired = await backend.expire_sessions()
                if expired:
                    deleted = await backend.delete_expired_sessions()
                    logger.info("background_cleanup_cycle", expired=expired, deleted=deleted)

                # Prune in-memory caches EVERY cycle (not only when a session
                # expired) so they cannot outgrow the active-session set. Pruned
                # state is recoverable: curator snapshot/briefing rebuild from the
                # graph and the dedupe/prefix caches rebuild on the next turn.
                try:
                    active = await backend.list_active_sessions()
                    active_ids = {s.get("session_id") for s in active if s.get("session_id")}
                    from archolith_proxy.curator.pipeline import prune_last_attempts
                    from archolith_proxy.curator.state import prune_session_state as prune_curator_state
                    from archolith_proxy.proxy.agent_solo import prune_session_state as prune_agent_solo_state

                    pruned_agent_solo = prune_agent_solo_state(active_ids)
                    pruned_curator = prune_curator_state(active_ids)
                    pruned_attempts = prune_last_attempts(active_ids)

                    # Evict idle event-driven curator workers (Phase 1).
                    pruned_workers = 0
                    try:
                        _cw_settings = get_settings()
                        if _cw_settings.curator_worker_enabled:
                            from archolith_proxy.curator.worker import shutdown_idle_curator_workers
                            pruned_workers = await shutdown_idle_curator_workers(
                                _cw_settings.curator_worker_idle_ttl_s
                            )
                    except Exception:
                        pass

                    if pruned_agent_solo or pruned_curator or pruned_attempts or pruned_workers:
                        logger.info(
                            "background_cache_pruned",
                            agent_solo_sessions=pruned_agent_solo,
                            curator_sessions=pruned_curator,
                            last_attempts=pruned_attempts,
                            curator_workers=pruned_workers,
                        )
                except Exception:
                    pass

            from archolith_proxy.proxy.locks import cleanup_stale_locks

            cleaned = cleanup_stale_locks()
            if cleaned:
                logger.info("background_lock_cleanup", locks_removed=cleaned)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("background_cleanup_error", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup, cleanup on shutdown."""
    settings = get_settings()
    record_start_time()

    proxy_missing = settings.check_required_for_proxy()
    if proxy_missing:
        logger.warning("missing_required_env_vars", vars=proxy_missing, note="proxy calls will fail")

    if not settings.admin_token:
        logger.warning(
            "admin_token_not_set",
            note=(
                "ADMIN_TOKEN is empty — admin endpoints are open to any loopback process. "
                "Set ADMIN_TOKEN for shared machines or network-exposed deployments."
            ),
        )

    # Log active profile with resolved flag bundle for operator clarity
    from archolith_proxy.config import PROFILES
    _profile = getattr(settings, "archolith_profile", "passthrough")
    _bundle = PROFILES.get(_profile, {})
    logger.info(
        "profile_active",
        profile=_profile,
        bundle=_bundle,
        note=f"ARCHOLITH_PROFILE={_profile} active",
    )

    # Loud startup check: the RTK_ENABLED env var was renamed to FILTER_ENABLED and is
    # no longer read. If a stale environment still sets RTK_ENABLED without FILTER_ENABLED,
    # filtering would silently turn off. Refuse to start rather than fail silently.
    if os.environ.get("RTK_ENABLED") is not None and os.environ.get("FILTER_ENABLED") is None:
        logger.error(
            "rtk_enabled_env_removed",
            note="RTK_ENABLED is no longer read; it was renamed to FILTER_ENABLED.",
        )
        raise RuntimeError(
            "RTK_ENABLED is set but is no longer read (renamed to FILTER_ENABLED). "
            "Filtering would silently be disabled. Set FILTER_ENABLED instead and unset RTK_ENABLED."
        )

    # Loud startup check: FILTER_ENABLED but the package missing from THIS env means
    # agent-solo compression and filtering silently no-op (the failure that
    # made the proxy look 100% passthrough on real sessions). Surface it now.
    if settings.filter_enabled:
        from archolith_proxy.filter_adapter import is_available as _filter_is_available
        if _filter_is_available():
            logger.info("filter_available", note="archolith_filter loaded")
        else:
            # Check whether filter_enabled came from the profile (graceful) or
            # from explicit env (fail-fast). A profile-driven enable degrades to
            # passthrough instead of refusing to start.
            _filter_from_profile = (
                "filter_enabled" not in getattr(settings, "model_fields_set", set())
                and _profile in PROFILES
                and "filter_enabled" in PROFILES.get(_profile, {})
            )
            if _filter_from_profile:
                logger.error(
                    "filter_unavailable_profile_degraded",
                    profile=_profile,
                    note=(
                        "ARCHOLITH_PROFILE enables filtering but archolith_filter is not "
                        "importable. Degrading to passthrough profile. "
                        "Install archolith-filter or set a different profile or explicit FILTER_ENABLED=false."
                    ),
                )
                # Full degradation to passthrough — clear all profile-enabled flags
                settings.archolith_profile = "passthrough"
                settings.filter_enabled = False
                settings.agent_solo_shrink_enabled = False
                settings.agent_solo_dedup_enabled = False
                settings.agent_solo_compress_middle_enabled = False
                settings.curator_enabled = False
                settings.background_pass_enabled = False
                settings.embedding_enabled = False
                settings.per_tool_extraction_enabled = False
                settings.session_recall_tool_enabled = False
            else:
                logger.error(
                    "filter_enabled_but_unavailable",
                    note="FILTER_ENABLED=true but archolith_filter is not importable; refusing to start.",
                )
                # Fail fast: a proxy that silently does no curation is worse than one
                # that won't boot. If filter is explicitly enabled it must be importable.
                raise RuntimeError(
                    "FILTER_ENABLED=true but archolith_filter is not importable in this environment. "
                    "Agent-solo compression and filter would silently do nothing. "
                    "Install it into the active venv (pip install -e ../archolith-filter) "
                    "or set FILTER_ENABLED=false."
                )

    graph_missing = settings.check_required_for_graph()
    if graph_missing:
        logger.info(
            "graph_features_missing_env",
            vars=graph_missing,
            note="set these to enable session graph + context assembly",
        )

    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
    )
    app.state.extractor_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
    )

    # Pre-warm tiktoken encoding so the first request doesn't pay the BPE table
    # load cost (100-500ms) inline on the event loop.
    try:
        import tiktoken
        await asyncio.to_thread(tiktoken.get_encoding, "cl100k_base")
        logger.info("tiktoken_prewarm_complete", encoding="cl100k_base")
    except Exception as _tiktoken_err:
        logger.warning("tiktoken_prewarm_failed", error=str(_tiktoken_err))

    # D4: track whether a configured graph backend ended up degraded so /health
    # can report it honestly instead of always returning 200.
    app.state.graph_degraded_reason = None

    if settings.graph_backend == "ladybug":
        try:
            from archolith_proxy.graph.ladybug_backend import LadybugBackend

            await init_backend(
                LadybugBackend(
                    db_path=settings.ladybug_db_path,
                    max_concurrent_queries=settings.ladybug_max_concurrent,
                )
            )
            logger.info("ladybug_initialized", db_path=settings.ladybug_db_path)
        except ImportError:
            app.state.graph_degraded_reason = "ladybug not installed"
            logger.error("ladybug_not_installed", note="pip install ladybug, or set GRAPH_BACKEND=neo4j")
        except Exception as e:
            app.state.graph_degraded_reason = f"ladybug init failed: {e}"
            logger.warning("ladybug_init_failed", error=str(e), note="proxy will run without graph features")
    elif settings.session_neo4j_password:
        await _init_neo4j_with_retry(
            settings,
            max_retries=settings.neo4j_max_retries,
            backoff_base=settings.neo4j_retry_backoff_base_s,
        )
    else:
        logger.warning("graph_not_configured", note="running in passthrough-only mode — set GRAPH_BACKEND=ladybug or SESSION_NEO4J_PASSWORD for graph features")

    # D4: a configured graph backend that did not come up is "degraded", not
    # "not_configured". Surface it; optionally fail closed.
    graph_configured = settings.graph_backend == "ladybug" or bool(settings.session_neo4j_password)
    if graph_configured and not is_graph_ready():
        reason = app.state.graph_degraded_reason or "graph backend configured but not ready after init"
        app.state.graph_degraded_reason = reason
        if settings.require_graph_on_startup:
            raise RuntimeError(f"Graph backend required but not ready: {reason}")
        logger.warning("graph_degraded", reason=reason)

    from archolith_proxy.proxy.live import get_live_stream

    app.state.live_stream = get_live_stream()

    app.state.trace_store = get_trace_store()
    if settings.trace_dir:
        loaded = await app.state.trace_store.load_from_disk()
        if loaded:
            logger.info("trace_history_restored", records=loaded)

    # Startup consistency check: verify trace turn numbers against graph
    if settings.trace_dir and is_graph_ready():
        try:
            report = await app.state.trace_store.verify_consistency()
            if report["orphans"] or report["mismatches"]:
                logger.warning(
                    "trace_graph_consistency_report",
                    orphans=len(report["orphans"]),
                    mismatches=len(report["mismatches"]),
                )
        except Exception:
            pass  # Non-fatal; trace/graph drift does not block startup

    from archolith_proxy.memory.registry import get_registry, reset_registry, init_plugins

    init_plugins()
    reset_registry()
    registry = get_registry()
    if settings.promotion_enabled:
        _load_memory_engines(settings, registry)
    elif settings.memory_engines_json:
        logger.info("memory_engines_configured_but_disabled", note="set PROMOTION_ENABLED=true to activate")

    from archolith_proxy.memory.promotion import PromotionService

    app.state.promotion_service = PromotionService(
        registry=registry,
        min_confidence=settings.promotion_min_confidence,
    )

    try:
        resp = await app.state.http_client.get(
            f"{settings.upstream_api_url}/models",
            headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
            timeout=5.0,
        )
        if resp.status_code < 500:
            logger.info("upstream_connectivity_ok", url=settings.upstream_api_url)
        else:
            logger.warning("upstream_connectivity_check_failed", status=resp.status_code)
    except Exception as e:
        logger.warning("upstream_connectivity_check_failed", error=str(e))

    from archolith_proxy.curator import configure_curation_mode

    configure_curation_mode()

    # ARC working set (Phase 4) — bound the in-memory caches to N sessions.
    # Registered BEFORE the persistence restore below so restore_caches seeds the
    # working set and the bound applies from the first reloaded session.
    if settings.curator_workingset_enabled:
        try:
            from archolith_proxy.curator.working_set import ARCWorkingSet
            from archolith_proxy.curator.state import set_working_set

            set_working_set(ARCWorkingSet(settings.curator_workingset_max_sessions))
            logger.info(
                "curator_workingset_enabled",
                max_sessions=settings.curator_workingset_max_sessions,
            )
        except Exception:
            logger.warning("curator_workingset_init_failed", exc_info=True)

    # Curator state durability (Phase 3) — reload persisted briefing/snapshot
    # caches, then register the write-through callback (AFTER restore so the
    # reload itself is not re-persisted).
    app.state.curator_state_persistence = None
    if settings.curator_state_persist_enabled:
        try:
            from archolith_proxy.curator.persistence import get_state_persistence
            from archolith_proxy.curator.state import restore_caches, set_persist_callback

            sp = get_state_persistence(settings.curator_state_persist_path)
            await sp.start()
            briefings, snapshots = await sp.load_all()
            restore_caches(briefings, snapshots)
            set_persist_callback(sp.enqueue)
            app.state.curator_state_persistence = sp
            logger.info(
                "curator_state_restored",
                briefings=len(briefings), snapshots=len(snapshots),
            )
        except Exception:
            logger.warning("curator_state_persistence_init_failed", exc_info=True)

    # Register built-in plugins before activation
    from archolith_proxy.plugins import get_plugin_registry
    from archolith_proxy.plugins.filter_plugin import FilterPlugin
    from archolith_proxy.plugins.memory_plugin import MemoryPlugin
    from archolith_proxy.plugins.audit_plugin import AuditPlugin

    plugin_registry = get_plugin_registry()
    plugin_registry.register(FilterPlugin())
    plugin_registry.register(MemoryPlugin())
    plugin_registry.register(AuditPlugin())

    # Activate all registered plugins (fail-safe — proxy always starts)
    plugin_results = await plugin_registry.activate_all()
    if plugin_results:
        active_count = sum(1 for ok in plugin_results.values() if ok)
        logger.info("plugins_activated", total=len(plugin_results), active=active_count)

    logger.info("proxy_starting", port=settings.proxy_port, upstream=settings.upstream_base_url)

    _cleanup_task = asyncio.create_task(_background_cleanup_loop())

    yield

    _cleanup_task.cancel()
    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass

    # Stop all event-driven curator workers (Phase 1).
    try:
        from archolith_proxy.curator.worker import shutdown_all_curator_workers
        await shutdown_all_curator_workers()
    except Exception:
        logger.warning("curator_worker_shutdown_failed", exc_info=True)

    # Flush and close curator state persistence (Phase 3).
    if getattr(app.state, "curator_state_persistence", None) is not None:
        try:
            from archolith_proxy.curator.state import set_persist_callback
            set_persist_callback(None)
            await app.state.curator_state_persistence.stop()
        except Exception:
            logger.warning("curator_state_persistence_stop_failed", exc_info=True)

    # Clear the ARC working set (Phase 4).
    if settings.curator_workingset_enabled:
        try:
            from archolith_proxy.curator.state import set_working_set
            set_working_set(None)
        except Exception:
            pass

    await plugin_registry.deactivate_all()

    from archolith_proxy.curator.tools import close_semantic_client
    await close_semantic_client()

    # Close all registered memory adapters
    from archolith_proxy.memory.registry import get_registry
    registry = get_registry()
    for adapter in registry.get_all_adapters():
        try:
            await adapter.close()
        except Exception as e:
            logger.warning("adapter_close_failed", adapter_id=adapter.config.id, error=str(e))

    await app.state.http_client.aclose()
    await app.state.extractor_client.aclose()
    await close_backend()
    logger.info("proxy_stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title="archolith-proxy",
        description="OpenAI-compatible proxy with graph-assembled context",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        start = time.monotonic()
        record_metric("total_requests")

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
        )

        response = await call_next(request)
        latency_ms = (time.monotonic() - start) * 1000

        logger.info(
            "request",
            status=response.status_code,
            latency_ms=round(latency_ms, 1),
        )
        return response

    # Mount route modules
    app.include_router(openai_router)
    app.include_router(metrics_router)
    app.include_router(admin_router, dependencies=[Depends(require_admin_token)])
    app.include_router(sessions_router, dependencies=[Depends(require_admin_token)])
    app.include_router(memory_admin_router, dependencies=[Depends(require_admin_token)])
    app.include_router(live_router)
    app.include_router(plugins_router, dependencies=[Depends(require_admin_token)])
    app.include_router(trace_router, dependencies=[Depends(require_admin_token)])

    # Dashboard static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/dashboard", StaticFiles(directory=str(static_dir), html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/dashboard/dashboard.html")

    # --- Health endpoints ---

    @app.get("/live")
    async def liveness() -> dict:
        """Liveness probe — is the process alive?"""
        return {
            "status": "alive",
            "version": __version__,
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0)
            if get_metrics()["start_time"]
            else 0,
        }

    @app.get("/ready")
    async def readiness(request: Request) -> dict:
        """Readiness probe — is the service ready to handle requests?"""
        ready = True
        reasons = []

        graph_status = "not_configured"
        if is_graph_ready():
            try:
                backend = get_backend()
                connected = await backend.verify_connectivity()
                graph_status = "connected" if connected else "disconnected"
                if not connected:
                    ready = False
                    reasons.append("graph_disconnected")
                    record_metric("neo4j_errors")
            except Exception:
                graph_status = "disconnected"
                ready = False
                reasons.append("graph_disconnected")
                record_metric("neo4j_errors")

        upstream_status = "unknown"
        try:
            settings = get_settings()
            resp = await request.app.state.http_client.get(
                f"{settings.upstream_api_url}/models",
                headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
                timeout=3.0,
            )
            if resp.status_code < 500:
                upstream_status = "ok"
            else:
                upstream_status = "degraded"
                ready = False
                reasons.append(f"upstream_{resp.status_code}")
        except Exception:
            upstream_status = "unreachable"
            ready = False
            reasons.append("upstream_unreachable")

        result = {
            "status": "ready" if ready else "not_ready",
            "graph": graph_status,
            "upstream": upstream_status,
            "version": __version__,
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0)
            if get_metrics()["start_time"]
            else 0,
        }
        if reasons:
            result["reasons"] = reasons

        if not ready:
            return JSONResponse(status_code=503, content=result)
        return result

    @app.get("/health")
    async def health(request: Request) -> dict:
        """Health endpoint. Reports degraded graph state honestly (D4).

        A graph backend that was configured but failed to initialize is reported
        as ``degraded`` (HTTP 503), distinct from an unconfigured graph (``ok``).
        """
        degraded_reason = getattr(request.app.state, "graph_degraded_reason", None)

        graph_status = "not_configured"
        if is_graph_ready():
            try:
                backend = get_backend()
                connected = await backend.verify_connectivity()
                graph_status = "connected" if connected else "disconnected"
            except Exception:
                graph_status = "disconnected"
                record_metric("neo4j_errors")
        elif degraded_reason:
            graph_status = "degraded"

        upstream_status = "unknown"
        try:
            settings = get_settings()
            resp = await request.app.state.http_client.get(
                f"{settings.upstream_api_url}/models",
                headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
                timeout=3.0,
            )
            upstream_status = "ok" if resp.status_code < 500 else "degraded"
        except Exception:
            upstream_status = "unreachable"

        result = {
            "status": "degraded" if degraded_reason else "ok",
            "graph": graph_status,
            "upstream": upstream_status,
            "version": __version__,
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0)
            if get_metrics()["start_time"]
            else 0,
        }
        if degraded_reason:
            result["graph_degraded_reason"] = degraded_reason
            return JSONResponse(status_code=503, content=result)
        return result

    return app


app = create_app()


def run() -> None:
    """Entry point for archolith-proxy CLI command.

    Starts uvicorn with settings from environment and config.
    """
    import uvicorn

    settings = get_settings()

    uvicorn.run(
        app=app,
        host=settings.proxy_host,
        port=settings.proxy_port,
        log_level="info",
    )
