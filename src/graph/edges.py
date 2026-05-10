"""Edge management — TOUCHES, MODIFIES, BELONGS_TO, SUPERSEDES."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import structlog

from src.graph.repository import CONTEXT_SESSION_LABEL, run_write
from src.models.graph_nodes import FileStatus

logger = structlog.get_logger()


async def create_belongs_to(session_id: str, fact_id: str) -> None:
    """Link a fact to its session."""
    cypher = f"""
    MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
    MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{fact_id: $fact_id}})
    MERGE (f)-[:BELONGS_TO]->(s)
    """
    await run_write(cypher, {"session_id": session_id, "fact_id": fact_id})


async def create_touches(session_id: str, file_path: str, status: FileStatus, turn: int) -> None:
    """Create or update a file-touch edge for the session."""
    cypher = f"""
    MERGE (f:{CONTEXT_SESSION_LABEL}:File {{path: $path, session_id: $session_id}})
    ON CREATE SET f.status = $status, f.last_read_turn = $turn
    ON MATCH SET
        f.status = CASE
            WHEN $status = 'modified' THEN 'modified'
            WHEN $status = 'created' THEN 'created'
            WHEN $status = 'deleted' THEN 'deleted'
            ELSE f.status
        END,
        f.last_modified_turn = CASE
            WHEN $status IN ['modified', 'created', 'deleted'] THEN $turn
            ELSE f.last_modified_turn
        END,
        f.last_read_turn = CASE
            WHEN $status = 'read' THEN $turn
            ELSE f.last_read_turn
        END
    WITH f
    MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})
    MERGE (s)-[:TOUCHES]->(f)
    """
    await run_write(cypher, {
        "path": file_path,
        "session_id": session_id,
        "status": status.value,
        "turn": turn,
    })


async def create_supersedes(old_fact_id: str, new_fact_id: str) -> None:
    """Link a new fact as superseding an old one."""
    cypher = f"""
    MATCH (old:{CONTEXT_SESSION_LABEL}:Fact {{fact_id: $old_id}})
    MATCH (new:{CONTEXT_SESSION_LABEL}:Fact {{fact_id: $new_id}})
    MERGE (new)-[:SUPERSEDES]->(old)
    """
    await run_write(cypher, {"old_id": old_fact_id, "new_id": new_fact_id})


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
