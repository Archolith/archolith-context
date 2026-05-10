"""FastAPI application factory with lifespan management."""

from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from src.graph.driver import close_driver, init_driver, ensure_indexes
from src.openai.router import router as openai_router

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup, cleanup on shutdown."""
    settings = get_settings()

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
        try:
            driver = await init_driver()
            app.state.neo4j_ready = True
            await ensure_indexes()
            logger.info("neo4j_initialized")
        except Exception as e:
            logger.warning("neo4j_init_failed", error=str(e), note="proxy will run without graph features")
    else:
        logger.info("neo4j_not_configured", note="set SESSION_NEO4J_PASSWORD to enable graph features")

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

    # Mount routes
    app.include_router(openai_router)

    # Health endpoint
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

        return {
            "status": "ok",
            "neo4j": neo4j_status,
        }

    # Metrics endpoint
    @app.get("/metrics")
    async def metrics() -> dict:
        return {
            "proxy": "cth.context-engine",
            "version": "0.1.0",
            "neo4j_ready": getattr(app.state, "neo4j_ready", False),
        }

    return app


app = create_app()
