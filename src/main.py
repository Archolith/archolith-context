"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.graph.driver import close_driver, init_driver, ensure_indexes
from src.logging_config import configure_logging
from src.openai.router import router as openai_router

# Configure structured JSON logging before first use
configure_logging()

logger = structlog.get_logger()

# --- In-memory metrics (process-level, reset on restart) ---
_metrics: dict = {
    "start_time": 0.0,
    "total_requests": 0,
    "assembly_modes": {"cold_start": 0, "graph": 0, "fallback": 0, "passthrough": 0},
    "extraction_successes": 0,
    "extraction_failures": 0,
    "upstream_errors": 0,
    "neo4j_errors": 0,
    "token_savings_estimated": 0,
    "total_input_tokens_seen": 0,
    "compaction_applied": 0,
}


async def _init_neo4j_with_retry(settings, max_retries: int = 3, backoff_base: float = 1.0) -> bool:
    """Initialize Neo4j with retry logic. Returns True if connected."""
    for attempt in range(max_retries):
        try:
            await init_driver()
            await ensure_indexes()
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup, cleanup on shutdown."""
    settings = get_settings()
    _metrics["start_time"] = time.time()

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

    # Neo4j — optional, graceful fallback if unavailable
    app.state.neo4j_ready = False
    if settings.session_neo4j_password:
        app.state.neo4j_ready = await _init_neo4j_with_retry(
            settings,
            max_retries=settings.neo4j_max_retries,
            backoff_base=settings.neo4j_retry_backoff_base_s,
        )
    else:
        logger.info("neo4j_not_configured", note="set SESSION_NEO4J_PASSWORD to enable graph features")

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

    yield

    # Cleanup
    await app.state.http_client.aclose()
    await app.state.extractor_client.aclose()
    await close_driver()
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
        _metrics["total_requests"] += 1

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

    # Mount routes
    app.include_router(openai_router)

    # --- Health endpoint ---
    @app.get("/health")
    async def health() -> dict:
        neo4j_status = "not_configured"
        if getattr(app.state, "neo4j_ready", False):
            from src.graph.driver import get_driver
            try:
                driver = await get_driver()
                await driver.verify_connectivity()
                neo4j_status = "connected"
            except Exception:
                neo4j_status = "disconnected"
                _metrics["neo4j_errors"] += 1

        # Check upstream (lightweight, with timeout)
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
            "neo4j": neo4j_status,
            "upstream": upstream_status,
            "version": "0.1.0",
            "uptime_s": round(time.time() - _metrics["start_time"], 0) if _metrics["start_time"] else 0,
        }

    # --- Metrics endpoint ---
    @app.get("/metrics")
    async def metrics() -> dict:
        active_sessions = 0
        if getattr(app.state, "neo4j_ready", False):
            try:
                from src.graph.cleanup import list_active_sessions
                sessions = await list_active_sessions()
                active_sessions = len(sessions)
            except Exception:
                pass

        # Derived rates
        total_extractions = _metrics["extraction_successes"] + _metrics["extraction_failures"]
        extraction_success_rate = (
            round(_metrics["extraction_successes"] / total_extractions, 4)
            if total_extractions > 0 else 0.0
        )
        avg_token_savings = (
            round(_metrics["token_savings_estimated"] / _metrics["total_requests"])
            if _metrics["total_requests"] > 0 else 0
        )
        total_input = _metrics["total_input_tokens_seen"]
        total_savings = _metrics["token_savings_estimated"]
        token_savings_rate = (
            round(total_savings / total_input, 4)
            if total_input > 0 else 0.0
        )

        return {
            "proxy": "cth.context-engine",
            "version": "0.1.0",
            "neo4j_ready": getattr(app.state, "neo4j_ready", False),
            "total_requests": _metrics["total_requests"],
            "assembly_modes": dict(_metrics["assembly_modes"]),
            "extraction_successes": _metrics["extraction_successes"],
            "extraction_failures": _metrics["extraction_failures"],
            "extraction_success_rate": extraction_success_rate,
            "upstream_errors": _metrics["upstream_errors"],
            "neo4j_errors": _metrics["neo4j_errors"],
            "active_sessions": active_sessions,
            "token_savings_estimated": _metrics["token_savings_estimated"],
            "avg_token_savings_per_request": avg_token_savings,
            "token_savings_rate": token_savings_rate,
        "total_input_tokens_seen": _metrics["total_input_tokens_seen"],
        "compaction_applied": _metrics["compaction_applied"],
        "uptime_s": round(time.time() - _metrics["start_time"], 0) if _metrics["start_time"] else 0,
        }

    # --- Sessions admin endpoints ---
    @app.get("/sessions")
    async def list_sessions() -> dict:
        """List all active sessions (admin endpoint)."""
        if not getattr(app.state, "neo4j_ready", False):
            return JSONResponse(status_code=503, content={"error": "Neo4j not available"})
        try:
            from src.graph.cleanup import list_active_sessions
            sessions = await list_active_sessions()
            return {"sessions": sessions, "count": len(sessions)}
        except Exception as e:
            logger.warning("sessions_list_failed", error=str(e))
            return JSONResponse(status_code=503, content={"error": f"Neo4j error: {e}"})

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        """Get session details (admin endpoint)."""
        if not getattr(app.state, "neo4j_ready", False):
            return JSONResponse(status_code=503, content={"error": "Neo4j not available"})
        try:
            from src.graph.cleanup import get_session_stats
            stats = await get_session_stats(session_id)
            if not stats:
                return JSONResponse(status_code=404, content={"error": f"Session {session_id} not found"})
            return {"session_id": session_id, **stats}
        except Exception as e:
            logger.warning("session_stats_failed", session_id=session_id, error=str(e))
            return JSONResponse(status_code=503, content={"error": f"Neo4j error: {e}"})

    return app


app = create_app()
