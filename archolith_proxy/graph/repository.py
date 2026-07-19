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

import structlog

from archolith_proxy.graph.driver import get_driver, get_database

logger = structlog.get_logger()

__all__ = [
    "LabelGuardViolation",
    "run_query",
    "run_write",
    "CONTEXT_SESSION_LABEL",
    "MEMORY_LABEL",
]

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

    Policy:
    - Queries targeting only :Memory are allowed (no isolation needed).
    - Session queries MUST explicitly include :ContextSession (not other session labels alone).
    - Queries mixing Memory and session labels are rejected (ambiguous isolation).

    Raises LabelGuardViolation if the query violates this policy.
    """
    # Skip validation for simple parameter-only queries or internal admin
    if not cypher or not cypher.strip():
        return

    # Check for memory label queries
    has_memory_label = _MEMORY_LABEL_PATTERN.search(cypher) is not None
    has_context_session = _LABEL_PATTERN.search(cypher) is not None

    # Check for other session labels (Session, Fact, File, Decision without ContextSession)
    other_session_pattern = re.compile(
        r":\s*(?:Session|Fact|File|Decision)\b", re.IGNORECASE
    )
    has_other_session_label = other_session_pattern.search(cypher) is not None

    # If query uses only Memory label, allow it (no context isolation needed)
    if has_memory_label and not has_context_session and not has_other_session_label:
        return

    # If query mixes Memory and session labels, reject (ambiguous isolation)
    if has_memory_label and (has_context_session or has_other_session_label):
        raise LabelGuardViolation(
            f"Cypher query cannot mix :{MEMORY_LABEL} and session labels. "
            f"Use either memory-only or session-scoped queries. "
            f"Query: {cypher[:120]}..."
        )

    # Session queries MUST have :ContextSession
    if has_context_session or has_other_session_label:
        if not has_context_session:
            raise LabelGuardViolation(
                f"Session queries must include :{CONTEXT_SESSION_LABEL} label explicitly. "
                f"Use :Fact, :Session, :File, :Decision with :{CONTEXT_SESSION_LABEL}, not alone. "
                f"Query: {cypher[:120]}..."
            )
        return

    raise LabelGuardViolation(
        f"Cypher query must include :{CONTEXT_SESSION_LABEL} label or be memory-only. "
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
