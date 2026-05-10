"""Label-guard repository layer for Neo4j queries.

All Neo4j queries must go through this layer, which auto-injects the
:ContextSession label into every MATCH clause. Queries without a label
scope will raise LabelGuardViolation.
"""

# Phase 2 implementation placeholder

CONTEXT_SESSION_LABEL = "ContextSession"


class LabelGuardViolation(Exception):
    """Raised when a Neo4j query lacks the required :ContextSession label."""
