"""Module-level graph backend singleton management.

Provides init/get/close/is_ready accessors that replace the previous
pattern of scattered getattr(request.app.state, "neo4j_ready", False)
checks and direct imports of init_driver/close_driver.

Usage:

    from src.graph.backend import init_backend, get_backend, is_graph_ready

    # During app lifespan startup:
    backend = Neo4jBackend(uri, user, password)
    await init_backend(backend)
    await backend.ensure_schema()

    # During request handling:
    if is_graph_ready():
        backend = get_backend()
        session = await backend.find_session_by_id(session_id)

    # During app lifespan shutdown:
    await close_backend()
"""

from __future__ import annotations

import structlog

from src.graph.protocol import GraphBackend

logger = structlog.get_logger()

# Module-level backend singleton
_backend: GraphBackend | None = None


async def init_backend(backend: GraphBackend) -> None:
    """Initialize the graph backend singleton.

    Calls backend.connect() and stores the backend instance.
    Replaces previous init_driver() + ensure_indexes() pattern.
    """
    global _backend
    await backend.connect()
    _backend = backend
    logger.info("graph_backend_initialized", backend_type=type(backend).__name__)


def get_backend() -> GraphBackend:
    """Return the shared backend instance.

    Raises RuntimeError if not initialized — callers should check
    is_graph_ready() first.
    """
    if _backend is None:
        raise RuntimeError(
            "Graph backend not initialized — call init_backend() first"
        )
    return _backend


async def close_backend() -> None:
    """Close the backend and release resources.

    Safe to call multiple times or when no backend was initialized.
    """
    global _backend
    if _backend is not None:
        try:
            await _backend.close()
        except Exception as e:
            logger.warning("graph_backend_close_error", error=str(e))
        _backend = None
        logger.info("graph_backend_closed")


def is_graph_ready() -> bool:
    """Check if the graph backend is connected and ready.

    Replaces the scattered getattr(request.app.state, "neo4j_ready", False)
    pattern used throughout the codebase. When all callers are migrated
    (Phase 4), the old app.state check will be removed.
    """
    if _backend is None:
        return False
    try:
        return _backend.is_ready()
    except Exception:
        return False
