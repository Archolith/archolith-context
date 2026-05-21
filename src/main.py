"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import get_settings
from src.graph.backend import close_backend, get_backend, init_backend, is_graph_ready
from src.graph.neo4j_backend import Neo4jBackend
from src.metrics import get_metrics, record_metric, record_start_time
from src.logging_config import configure_logging
from src.openai.router import router as openai_router
from src.trace.router import router as trace_router
from src.trace.store import get_trace_store
from src.admin import require_admin_token

# Configure structured JSON logging before first use
configure_logging()

logger = structlog.get_logger()




def _load_memory_engines(settings, registry) -> None:
    """Load memory engine configs from env and register them."""
    import json

    from src.memory.models import MemoryEngineConfig

    # If MEMORY_ENGINES_JSON is set, parse and register each engine
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
    else:
        # Fallback: auto-register cth-memory from legacy settings if promotion is enabled
        if settings.memory_api_url:
            registry.register(
                MemoryEngineConfig(
                    id="cth-memory",
                    type="cth_mcp_memory",
                    enabled=True,
                    priority=10,
                    base_url=settings.memory_api_url,
                    api_key_env="MEMORY_API_KEY",
                )
            )
            logger.info("memory_engine_auto_registered", engine_id="cth-memory", base_url=settings.memory_api_url)


async def _init_neo4j_with_retry(settings, max_retries: int = 3, backoff_base: float = 1.0) -> bool:
    """Initialize Neo4j backend with retry logic. Returns True if connected."""
    for attempt in range(max_retries):
        try:
            await init_backend(Neo4jBackend())
            logger.info("neo4j_initialized", attempt=attempt + 1)
            return True
        except Exception as e:
            if attempt < max_retries - 1:
                delay = backoff_base * (2 ** attempt)
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
    """Hourly background task: expire sessions, delete expired, prune locks."""
    while True:
        try:
            await asyncio.sleep(3600)  # Every hour
            if is_graph_ready():
                backend = get_backend()
                expired = await backend.expire_sessions()
                if expired:
                    deleted = await backend.delete_expired_sessions()
                    logger.info("background_cleanup_cycle", expired=expired, deleted=deleted)
            # Prune stale session locks
            from src.proxy.locks import cleanup_stale_locks
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

    # Fail-fast: warn about missing required keys
    proxy_missing = settings.check_required_for_proxy()
    if proxy_missing:
        logger.warning("missing_required_env_vars", vars=proxy_missing, note="proxy calls will fail")

    graph_missing = settings.check_required_for_graph()
    if graph_missing:
        logger.info(
            "graph_features_missing_env",
            vars=graph_missing,
            note="set these to enable session graph + context assembly",
        )

    # HTTP clients (shared connection pools)
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
    )
    app.state.extractor_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
    )

    # Graph backend — optional, graceful fallback if unavailable
    if settings.graph_backend == "ladybug":
        try:
            from src.graph.ladybug_backend import LadybugBackend
            await init_backend(LadybugBackend(
                db_path=settings.ladybug_db_path,
                max_concurrent_queries=settings.ladybug_max_concurrent,
            ))
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

    # Live stream broadcaster (WebSocket pub/sub)
    from src.proxy.live import get_live_stream
    app.state.live_stream = get_live_stream()

    # Turn trace store (in-memory per-turn inspection)
    from src.trace.store import get_trace_store
    app.state.trace_store = get_trace_store()

    # Memory engine registry — load from config if promotion enabled
    from src.memory.registry import get_registry, reset_registry
    from src.memory.models import MemoryEngineConfig
    reset_registry()
    registry = get_registry()
    if settings.promotion_enabled:
        _load_memory_engines(settings, registry)
    elif settings.memory_engines_json:
        logger.info("memory_engines_configured_but_disabled", note="set PROMOTION_ENABLED=true to activate")

    # Promotion service
    from src.memory.promotion import PromotionService
    app.state.promotion_service = PromotionService(
        registry=registry,
        min_confidence=settings.promotion_min_confidence,
    )

    # Optional: check upstream connectivity (warn-only, not fatal)
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

    logger.info("proxy_starting", port=settings.proxy_port, upstream=settings.upstream_base_url)

    # Background cleanup loop — hourly session expiry + lock pruning
    _cleanup_task = asyncio.create_task(_background_cleanup_loop())

    yield

    # Cancel background cleanup
    _cleanup_task.cancel()
    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass

    # Cleanup
    await app.state.http_client.aclose()
    await app.state.extractor_client.aclose()
    await close_backend()
    logger.info("proxy_stopped")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="cth.context-engine",
        description="OpenAI-compatible proxy with graph-assembled context",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — allow all origins for local development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request-level logging middleware (includes session context via structlog context vars)
    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        start = time.monotonic()
        record_metric("total_requests")

        # Bind request-level context that the handler may enrich
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            method=request.method,
            path=request.url.path,
        )

        response = await call_next(request)
        latency_ms = (time.monotonic() - start) * 1000

        # Log with whatever context the handler bound (session_id, turn, assembly_mode)
        logger.info(
            "request",
            status=response.status_code,
            latency_ms=round(latency_ms, 1),
        )
        return response

    # Mount routes — openai_router is unauthenticated (proxy surface)
    app.include_router(openai_router)
    # trace_router is an operator surface — protect with admin token
    app.include_router(trace_router, dependencies=[Depends(require_admin_token)])

    # Dashboard static files (serve /dashboard/ -> src/static/)
    import os
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/dashboard", StaticFiles(directory=static_dir, html=True), name="dashboard")

    # Redirect root to dashboard
    from fastapi.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/dashboard/dashboard.html")

    # --- Health endpoints (liveness + readiness + legacy) ---

    @app.get("/live")
    async def liveness() -> dict:
        """Liveness probe — is the process alive?

        Always returns 200 while the process is running. Does NOT
        check upstream or Neo4j connectivity. Use /ready for that.
        """
        return {
            "status": "alive",
            "version": "0.1.0",
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0) if get_metrics()["start_time"] else 0,
        }

    @app.get("/ready")
    async def readiness() -> dict:
        """Readiness probe — is the service ready to handle requests?

        Checks graph backend and upstream connectivity. Returns 503
        when upstream is unreachable or graph is disconnected, but
        the process stays alive (liveness unaffected).
        """
        ready = True
        reasons = []

        # Check graph backend (works for both Neo4j and LadybugDB)
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

        # Check upstream (lightweight, with timeout)
        upstream_status = "unknown"
        try:
            settings = get_settings()
            resp = await app.state.http_client.get(
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
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0) if get_metrics()["start_time"] else 0,
        }
        if reasons:
            result["reasons"] = reasons

        if not ready:
            return JSONResponse(status_code=503, content=result)
        return result

    @app.get("/health")
    async def health() -> dict:
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
            resp = await app.state.http_client.get(
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
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0) if get_metrics()["start_time"] else 0,
        }

    # --- Metrics endpoint ---
    @app.get("/metrics")
    async def metrics() -> dict:
        active_sessions = 0
        if is_graph_ready():
            try:
                from src.graph.session import list_active_sessions
                sessions = await list_active_sessions()
                active_sessions = len(sessions)
            except Exception:
                pass

        # Derived rates
        total_extractions = get_metrics()["extraction_successes"] + get_metrics()["extraction_failures"]
        extraction_success_rate = (
            round(get_metrics()["extraction_successes"] / total_extractions, 4)
            if total_extractions > 0 else 0.0
        )
        avg_token_savings = (
            round(get_metrics()["token_savings_estimated"] / get_metrics()["total_requests"])
            if get_metrics()["total_requests"] > 0 else 0
        )
        total_input = get_metrics()["total_input_tokens_seen"]
        total_savings = get_metrics()["token_savings_estimated"]
        token_savings_rate = (
            round(total_savings / total_input, 4)
            if total_input > 0 else 0.0
        )

        return {
            "proxy": "cth.context-engine",
            "version": "0.1.0",
            "graph_ready": is_graph_ready(),
            "total_requests": get_metrics()["total_requests"],
            "assembly_modes": dict(get_metrics()["assembly_modes"]),
            "extraction_successes": get_metrics()["extraction_successes"],
            "extraction_failures": get_metrics()["extraction_failures"],
            "extraction_success_rate": extraction_success_rate,
            "upstream_errors": get_metrics()["upstream_errors"],
            "neo4j_errors": get_metrics()["neo4j_errors"],
            "active_sessions": active_sessions,
            "token_savings_estimated": get_metrics()["token_savings_estimated"],
            "avg_token_savings_per_request": avg_token_savings,
            "token_savings_rate": token_savings_rate,
            "total_input_tokens_seen": get_metrics()["total_input_tokens_seen"],
            "compaction_applied": get_metrics()["compaction_applied"],
            "trace_records": getattr(app.state, "trace_store", get_trace_store()).total_traces,
            "trace_sessions": getattr(app.state, "trace_store", get_trace_store()).session_count,
            "uptime_s": round(time.time() - get_metrics()["start_time"], 0) if get_metrics()["start_time"] else 0,
        }

    # --- Sessions admin endpoints ---
    @app.get("/sessions")
    async def list_sessions(admin: None = Depends(require_admin_token)) -> dict:
        """List all active sessions (admin endpoint)."""
        if not is_graph_ready():
            return JSONResponse(status_code=503, content={"error": "Neo4j not available"})
        try:
            from src.graph.session import list_active_sessions
            sessions = await list_active_sessions()
            return {"sessions": sessions, "count": len(sessions)}
        except Exception as e:
            logger.warning("sessions_list_failed", error=str(e))
            return JSONResponse(status_code=503, content={"error": f"Neo4j error: {e}"})

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str, admin: None = Depends(require_admin_token)) -> dict:
        """Get session details (admin endpoint)."""
        if not is_graph_ready():
            return JSONResponse(status_code=503, content={"error": "Neo4j not available"})
        try:
            from src.graph.session import get_session_stats
            stats = await get_session_stats(session_id)
            if not stats:
                return JSONResponse(status_code=404, content={"error": f"Session {session_id} not found"})
            return {"session_id": session_id, **stats}
        except Exception as e:
            logger.warning("session_stats_failed", session_id=session_id, error=str(e))
            return JSONResponse(status_code=503, content={"error": f"Neo4j error: {e}"})

    # --- Memory engine & promotion admin endpoints ---

    @app.get("/memory-engines")
    async def list_memory_engines(admin: None = Depends(require_admin_token)) -> dict:
        """List all configured memory engines and their health."""
        from src.memory.registry import get_registry

        registry = get_registry()
        engines = registry.list_engines()
        health = await registry.healthcheck_all()
        return {
            "engines": engines,
            "health": health,
            "default_engine_id": registry.default_engine_id,
            "promotion_enabled": get_settings().promotion_enabled,
        }

    @app.get("/memory-engines/{engine_id}")
    async def get_memory_engine(engine_id: str, admin: None = Depends(require_admin_token)) -> dict:
        """Get details and health for a specific memory engine."""
        from src.memory.registry import get_registry

        registry = get_registry()
        config = registry.get_config(engine_id)
        if config is None:
            return JSONResponse(status_code=404, content={"error": f"Engine {engine_id} not found"})
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

    @app.get("/promotions")
    async def list_promotions(admin: None = Depends(require_admin_token)) -> dict:
        """List promotion history and stats."""
        svc = getattr(app.state, "promotion_service", None)
        if svc is None:
            return JSONResponse(status_code=503, content={"error": "Promotion service not initialized"})
        return {
            "stats": svc.stats,
            "recent": [r.model_dump(mode="json") for r in svc.audit_trail[-50:]],
        }

    @app.post("/promotions/retry/{promotion_id}")
    async def retry_promotion(promotion_id: str, admin: None = Depends(require_admin_token)) -> dict:
        """Retry a failed promotion by its promotion_id (finds it in audit trail)."""
        svc = getattr(app.state, "promotion_service", None)
        if svc is None:
            return JSONResponse(status_code=503, content={"error": "Promotion service not initialized"})
        # Find the original record in the audit trail
        original = None
        for r in svc.audit_trail:
            if r.promotion_id == promotion_id:
                original = r
                break
        if original is None:
            return JSONResponse(status_code=404, content={"error": f"Promotion {promotion_id} not found"})
        return JSONResponse(status_code=200, content={"note": "Retry requires resubmission with the original PromotionRecord"})

    # --- WebSocket live stream endpoint ---
    @app.websocket("/ws/stream")
    async def ws_live_stream(websocket: WebSocket) -> None:
        """WebSocket endpoint for real-time proxy event streaming.

        Clients connect and receive JSON events for every request, assembly,
        response, extraction, and recall event that flows through the proxy.
        Slow clients are disconnected after 256 queued events.

        When ADMIN_TOKEN is set, clients must provide it via query param
        ?token=<value> or the connection is closed.
        """
        settings = get_settings()
        if settings.admin_token:
            token = websocket.query_params.get("token", "")
            if token != settings.admin_token:
                await websocket.close(code=4001, reason="Invalid admin token")
                return

        await websocket.accept()
        live_stream = getattr(app.state, "live_stream", None)
        if not live_stream:
            await websocket.close(code=1011, reason="Live stream not initialized")
            return

        q = await live_stream.subscribe()
        logger.info("live_stream_client_connected", subscribers=live_stream.subscriber_count)

        try:
            while True:
                event = await q.get()
                if event.get("type") == "dropped":
                    await websocket.send_json(event)
                    await websocket.close(code=1008, reason="Queue overflow - too slow")
                    break
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("live_stream_client_error", error=str(e))
        finally:
            await live_stream.unsubscribe(q)
            logger.info("live_stream_client_disconnected", subscribers=live_stream.subscriber_count)

    return app


app = create_app()
