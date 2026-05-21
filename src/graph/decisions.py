"""Decision node CRUD — stores agent decisions and retrieves them.

Decision nodes track architectural choices, design decisions, and other
agent-deliberated conclusions. They are session-scoped via :ContextSession.

Moved `store_decision` from edges.py (it creates a Decision node + BELONGS_TO
edge, not an edge operation). Added `get_decisions` absorbed from inline
Cypher in assembler/context.py and trace/router.py.
"""

from __future__ import annotations

from uuid import uuid4

import structlog

from src.graph.repository import CONTEXT_SESSION_LABEL, run_query, run_write

logger = structlog.get_logger()


async def store_decision(
    session_id: str,
    summary: str,
    rationale: str | None,
    turn: int,
) -> str:
    """Store a decision node and link to session."""
    decision_id = uuid4().hex[:16]
    cypher = f"""
    CREATE (d:{CONTEXT_SESSION_LABEL}:Decision {{
        decision_id: $decision_id,
        session_id: $session_id,
        summary: $summary,
        rationale: $rationale,
        turn: $turn,
        superseded_by: null
    }})
    WITH d
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
MERGE (d)-[:BELONGS_TO]->(s)
    RETURN d.decision_id
    """
    results = await run_write(cypher, {
        "decision_id": decision_id,
        "session_id": session_id,
        "summary": summary,
        "rationale": rationale,
        "turn": turn,
    })
    return results[0]["d.decision_id"] if results else decision_id


async def get_decisions(
    session_id: str,
    include_superseded: bool = False,
) -> list[dict]:
    """Get all decisions for a session.

    Args:
        session_id: The session to query.
        include_superseded: If True, include superseded decisions (default False).
    """
    superseded_filter = "" if include_superseded else "WHERE d.superseded_by IS NULL"
    cypher = f"""
        MATCH (d:{CONTEXT_SESSION_LABEL}:Decision {{session_id: $session_id}})
        {superseded_filter}
        RETURN d.decision_id AS decision_id, d.summary AS summary,
               d.rationale AS rationale, d.turn AS turn,
               d.superseded_by AS superseded_by
        ORDER BY d.turn ASC
    """
    results = await run_query(cypher, {"session_id": session_id})
    decisions = [
        {
            "decision_id": r["decision_id"],
            "summary": r["summary"],
            "rationale": r.get("rationale"),
            "turn": r["turn"],
            "superseded_by": r.get("superseded_by"),
        }
        for r in results
    ]
    return decisions
