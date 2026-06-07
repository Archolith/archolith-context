"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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
                    if pruned_agent_solo or pruned_curator or pruned_attempts:
                        logger.info(
                            "background_cache_pruned",
                            agent_solo_sessions=pruned_agent_solo,
                            curator_sessions=pruned_curator,
                            last_attempts=pruned_attempts,
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

    # Loud startup check: FILTER_ENABLED but the package missing from THIS env means
    # agent-solo compression and filtering silently no-op (the failure that
    # made the proxy look 100% passthrough on real sessions). Surface it now.
    if settings.filter_enabled:
        from archolith_proxy.filter_adapter import is_available as _rtk_is_available
        if _rtk_is_available():
            logger.info("rtk_available", note="archolith_filter loaded")
        else:
            logger.error(
                "rtk_enabled_but_unavailable",
                note="RTK_ENABLED=true but archolith_filter is not importable; refusing to start.",
            )
            # Fail fast: a proxy that silently does no curation is worse than one
            # that won't boot. If RTK is explicitly enabled it must be importable.
            raise RuntimeError(
                "RTK_ENABLED=true but archolith_filter is not importable in this environment. "
                "Agent-solo compression and RTK filtering would silently do nothing. "
                "Install it into the active venv (pip install -e ../archolith-filter) "
                "or set RTK_ENABLED=false."
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
            logger.error("ladybug_not_installed", note="pip install ladybug, or set GRAPH_BACKEND=neo4j")
        except Exception as e:
            logger.warning("ladybug_init_failed", error=str(e), note="proxy will run without graph features")
    elif settings.session_neo4j_password:
        await _init_neo4j_with_retry(
            settings,
            max_retries=settings.neo4j_max_retries,
            backoff_base=settings.neo4j_retry_backoff_base_s,
        )
    else:
        logger.info("graph_not_configured", note="set GRAPH_BACKEND=ladybug or SESSION_NEO4J_PASSWORD for graph features")

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

    logger.info("proxy_starting", port=settings.proxy_port, upstream=settings.upstream_base_url)

    _cleanup_task = asyncio.create_task(_background_cleanup_loop())

    yield

    _cleanup_task.cancel()
    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass

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
    app = FastAPI(
        title="archolith-proxy",
        description="OpenAI-compatible proxy with graph-assembled context",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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
            "version": "0.1.0",
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
            "version": "0.1.0",
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
        """Legacy health endpoint (compatibility). Delegates to readiness."""
        graph_status = "not_configured"
        if is_graph_ready():
            try:
                backend = get_backend()
                connected = await backend.verify_connectivity()
                graph_status = "connected" if connected else "disconnected"
            except Exception:
                graph_status = "disconnected"
                record_metric("neo4j_errors")

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

        return {
            "status": "ok",
            "graph": graph_status,
            "upstream": upstream_status,
            "version": "0.1.0",
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0)
            if get_metrics()["start_time"]
            else 0,
        }

    return app


app = create_app()
