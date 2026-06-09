"""Neo4j connection pool — label-based isolation in default database.

Uses the existing Neo4j instance (Community Edition) with the default
`neo4j` database. All session nodes carry the :ContextSession label.
"""

from __future__ import annotations


import neo4j
import structlog

from archolith_proxy.config import get_settings

logger = structlog.get_logger()

__all__ = [
    "init_driver",
    "get_driver",
    "is_connected",
    "close_driver",
    "get_database",
    "ensure_indexes",
]

# Shared driver — initialized once in app lifespan
_driver: neo4j.AsyncDriver | None = None


async def init_driver() -> neo4j.AsyncDriver:
    """Initialize the Neo4j async driver."""
    global _driver
    settings = get_settings()
    _driver = neo4j.AsyncGraphDatabase.driver(
        settings.session_neo4j_uri,
        auth=(settings.session_neo4j_user, settings.session_neo4j_password),
    )
    await _driver.verify_connectivity()
    logger.info("neo4j_connected", uri=settings.session_neo4j_uri, database=settings.session_neo4j_database)
    return _driver


async def get_driver() -> neo4j.AsyncDriver:
    """Return the shared driver. Raises if not initialized."""
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialized — call init_driver() first")
    return _driver


def is_connected() -> bool:
    """Check if the Neo4j driver is initialized and connected.

    Non-async check: verifies initialization state only, not connectivity.
    Use verify_connectivity() for a full health check.
    """
    return _driver is not None


async def close_driver() -> None:
    """Close the shared driver."""
    global _driver
    if _driver:
        await _driver.close()
        _driver = None
        logger.info("neo4j_disconnected")


def get_database() -> str:
    """Return the configured Neo4j database name."""
    return get_settings().session_neo4j_database


# --- Index creation (run once on startup) ---

CREATE_INDEXES_CYPHER = """
CREATE CONSTRAINT session_id_unique IF NOT EXISTS
FOR (n:Session) REQUIRE n.session_id IS UNIQUE;

CREATE CONSTRAINT session_fingerprint_unique IF NOT EXISTS
FOR (n:Session) REQUIRE n.fingerprint IS UNIQUE;

CREATE INDEX fact_type_idx IF NOT EXISTS
FOR (n:Fact) ON (n.fact_type);

CREATE INDEX fact_valid_until_idx IF NOT EXISTS
FOR (n:Fact) ON (n.valid_until);

CREATE INDEX fact_session_id_idx IF NOT EXISTS
FOR (n:Fact) ON (n.session_id);

CREATE INDEX file_session_id_idx IF NOT EXISTS
FOR (n:File) ON (n.session_id);

CREATE INDEX session_last_active_idx IF NOT EXISTS
FOR (n:Session) ON (n.last_active);

CREATE INDEX session_status_idx IF NOT EXISTS
FOR (n:Session) ON (n.status);

CREATE INDEX fact_invalidated_at_idx IF NOT EXISTS
FOR (n:Fact) ON (n.invalidated_at);

CREATE INDEX decision_session_id_idx IF NOT EXISTS
FOR (n:Decision) ON (n.session_id);
"""


async def ensure_indexes() -> None:
    """Create required indexes and constraints. Safe to run on every startup."""
    driver = await get_driver()
    db = get_database()
    async with driver.session(database=db) as session:
        for statement in CREATE_INDEXES_CYPHER.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    await session.run(stmt)
                except Exception as e:
                    # Constraints may already exist — that's fine
                    if "already exists" not in str(e).lower():
                        logger.warning("index_creation_warning", statement=stmt[:80], error=str(e))
    logger.info("neo4j_indexes_ensured")
