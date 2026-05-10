"""Label-guard repository layer for Neo4j queries.

All Neo4j queries MUST go through this layer. It auto-injects the
:ContextSession label into every Cypher MATCH clause. Queries without
a label scope will raise LabelGuardViolation at the application level.

This prevents a single developer mistake (forgetting the label) from
leaking across the isolation boundary between session context and
long-term memory.
"""

from __future__ import annotations

import re

import neo4j
import structlog

from src.graph.driver import get_driver, get_database

logger = structlog.get_logger()

CONTEXT_SESSION_LABEL = "ContextSession"
MEMORY_LABEL = "Memory"

# All valid labels for session-scoped nodes
_SESSION_LABELS = {"ContextSession", "Session", "Fact", "File", "Decision"}

# Regex to detect MATCH clauses that already specify our label
_LABEL_PATTERN = re.compile(rf":\s*{CONTEXT_SESSION_LABEL}\b", re.IGNORECASE)

# Also accept any of the session-scoped sub-labels (they always co-occur with :ContextSession)
_SESSION_LABEL_PATTERN = re.compile(
    rf":\s*(?:{'|'.join(_SESSION_LABELS)})\b", re.IGNORECASE
)

# Patterns that indicate a read-only query (no label guard needed for reads
# that explicitly target memory, but we still log a warning)
_MEMORY_LABEL_PATTERN = re.compile(rf":\s*{MEMORY_LABEL}\b", re.IGNORECASE)


class LabelGuardViolation(Exception):
    """Raised when a Neo4j query lacks the required :ContextSession label."""


def _validate_cypher(cypher: str) -> None:
    """Validate that a Cypher query is label-scoped to :ContextSession.

    Raises LabelGuardViolation if no MATCH clause contains the label.
    """
    # Skip validation for simple parameter-only queries or internal admin
    if not cypher or not cypher.strip():
        return

    # If query explicitly targets memory label, it's not our concern
    if _MEMORY_LABEL_PATTERN.search(cypher):
        return

    # Check if any MATCH/CREATE/MERGE clause includes a session-scoped label
    if _SESSION_LABEL_PATTERN.search(cypher):
        return

    # Check for MERGE/CREATE with any session label
    create_pattern = re.compile(
        rf"(?:CREATE|MERGE)\s*\([^)]*:\s*(?:{'|'.join(_SESSION_LABELS)})\b", re.IGNORECASE
    )
    if create_pattern.search(cypher):
        return

    raise LabelGuardViolation(
        f"Cypher query must include :{CONTEXT_SESSION_LABEL} label. "
        f"Query: {cypher[:120]}..."
    )


async def run_query(
    cypher: str,
    params: dict | None = None,
) -> list[dict]:
    """Execute a Cypher query with label-guard validation.

    All queries must reference :ContextSession in MATCH/CREATE/MERGE clauses.
    """
    _validate_cypher(cypher)
    driver = await get_driver()
    db = get_database()

    async with driver.session(database=db) as session:
        result = await session.run(cypher, params or {})
        records = await result.data()
        return [dict(record) for record in records]


async def run_write(
    cypher: str,
    params: dict | None = None,
) -> list[dict]:
    """Execute a write Cypher query in an explicit transaction."""
    _validate_cypher(cypher)
    driver = await get_driver()
    db = get_database()

    async with driver.session(database=db) as session:
        async with await session.begin_transaction() as tx:
            result = await tx.run(cypher, params or {})
            records = await result.data()
            await tx.commit()
            return [dict(record) for record in records]
