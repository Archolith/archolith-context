"""Fact CRUD + vector similarity queries."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import json

import structlog

from archolith_proxy.shared.text_utils import jaccard_similarity
from archolith_proxy.config import get_settings
from archolith_proxy.graph.repository import CONTEXT_SESSION_LABEL, run_query, run_write
from archolith_proxy.models.graph_nodes import FactType

logger = structlog.get_logger()

__all__ = [
    "store_fact",
    "store_facts_batch",
    "invalidate_facts",
    "find_matching_fact_ids",
    "get_active_facts",
    "get_active_fact_count",
    "get_facts_filtered",
    "get_supersession_chain",
    "get_invalidated_facts",
]

# Similarity threshold for matching invalidated descriptions to existing facts
_INVALIDATION_MATCH_THRESHOLD = 0.60


def _decode_fact(fact: dict) -> dict:
    """Expose nullable structured JSON as a dict while keeping old facts valid."""
    raw = fact.get("structured_json")
    if raw:
        try:
            fact["structured"] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            fact["structured"] = None
    fact.pop("structured_json", None)
    return fact


async def store_fact(
    session_id: str,
    content: str,
    fact_type: FactType,
    source_turn: int,
    confidence: float = 0.5,
    embedding: list[float] | None = None,
    source_tool: str | None = None,
    structured: dict | None = None,
) -> str:
    """Store a single fact in the session graph."""
    fact_id = uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat()

    embedding_prop = "null" if embedding is None else "$embedding"
    params = {
        "fact_id": fact_id,
        "session_id": session_id,
        "content": content,
        "fact_type": fact_type.value,
        "valid_from": now,
        "confidence": confidence,
        "source_turn": source_turn,
        "source_tool": source_tool,
        "structured_json": json.dumps(structured, separators=(",", ":")) if structured else None,
    }
    if embedding is not None:
        params["embedding"] = embedding

    cypher = f"""
        CREATE (f:{CONTEXT_SESSION_LABEL}:Fact {{
        fact_id: $fact_id,
        session_id: $session_id,
        content: $content,
        fact_type: $fact_type,
        valid_from: datetime($valid_from),
        valid_until: null,
        invalidated_at: null,
        confidence: $confidence,
        source_turn: $source_turn,
        embedding: {embedding_prop},
        source_tool: $source_tool,
        structured_json: $structured_json
        }})
        RETURN f.fact_id
        """
    results = await run_write(cypher, params)
    return results[0]["f.fact_id"] if results else fact_id


async def store_facts_batch(
    session_id: str,
    facts: list[dict],
    source_turn: int,
) -> list[str]:
    """Store multiple facts in a single transaction using UNWIND.

    This replaces the previous per-fact loop with a true batch write.
    All facts are created atomically in one Neo4j transaction.
    """
    if not facts:
        return []

    now = datetime.now(timezone.utc).isoformat()

    # Build the fact rows for UNWIND
    rows = []
    for fact in facts:
        fact_id = uuid4().hex[:16]
        embedding = fact.get("embedding")
        rows.append({
            "fact_id": fact_id,
            "content": fact.get("content", ""),
            "fact_type": fact.get("fact_type", "observation"),
            "confidence": fact.get("confidence", 0.5),
            "embedding": embedding,
            "source_tool": fact.get("source_tool"),
            "structured_json": json.dumps(fact.get("structured"), separators=(",", ":"))
                if isinstance(fact.get("structured"), dict) else None,
        })

    cypher = f"""
    UNWIND $rows AS row
    CREATE (f:{CONTEXT_SESSION_LABEL}:Fact {{
        fact_id: row.fact_id,
        session_id: $session_id,
        content: row.content,
        fact_type: row.fact_type,
        valid_from: datetime($now),
        valid_until: null,
        invalidated_at: null,
        confidence: row.confidence,
        source_turn: $source_turn,
        embedding: row.embedding,
        source_tool: row.source_tool,
        structured_json: row.structured_json
    }})
    RETURN f.fact_id AS fact_id
    """

    results = await run_write(cypher, {
        "rows": rows,
        "session_id": session_id,
        "source_turn": source_turn,
        "now": now,
    })

    # Return fact_ids in the same order as input
    result_ids = [r["fact_id"] for r in results]
    return result_ids if result_ids else [row["fact_id"] for row in rows]


async def invalidate_facts(fact_ids: list[str]) -> int:
    """Set valid_until on facts, marking them superseded."""
    if not fact_ids:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    cypher = f"""
MATCH (f:{CONTEXT_SESSION_LABEL}:Fact)
WHERE f.fact_id IN $fact_ids AND f.valid_until IS NULL
SET f.valid_until = datetime($now), f.invalidated_at = datetime($now)
RETURN count(f) AS invalidated
"""
    results = await run_write(cypher, {"fact_ids": fact_ids, "now": now})
    return results[0]["invalidated"] if results else 0


async def find_matching_fact_ids(
    session_id: str,
    descriptions: list[str],
    threshold: float = _INVALIDATION_MATCH_THRESHOLD,
) -> list[str]:
    """Match invalidated-fact description strings to actual fact IDs.

    The extraction model returns description strings like "The build error
    on line 42 was fixed", not hex fact IDs. This function fetches active
    facts for the session and uses Jaccard similarity to find the best
    match for each description.

    Args:
        session_id: The session whose facts to search.
        descriptions: Description strings from the extractor's "invalidated" list.
        threshold: Minimum Jaccard similarity to consider a match (default 0.60).

    Returns:
        List of fact_ids that match the descriptions (deduplicated).
    """
    if not descriptions:
        return []

    active_facts = await get_active_facts(session_id, limit=get_settings().fact_pool_limit)
    if not active_facts:
        return []

    matched_ids: set[str] = set()
    for desc in descriptions:
        best_id: str | None = None
        best_sim: float = 0.0
        for fact in active_facts:
            content = fact.get("content", "")
            if not content:
                continue
            sim = jaccard_similarity(desc, content)
            if sim > best_sim and sim >= threshold:
                best_sim = sim
                best_id = fact.get("fact_id")

        if best_id:
            matched_ids.add(best_id)
            logger.debug(
                "invalidation_matched",
                session_id=session_id,
                description=desc[:60],
                matched_fact_id=best_id,
                similarity=round(best_sim, 3),
            )

    if matched_ids:
        logger.info(
            "invalidation_matches_found",
            session_id=session_id,
            descriptions=len(descriptions),
            matched=len(matched_ids),
        )

    return list(matched_ids)


async def get_active_facts(session_id: str, limit: int = 50) -> list[dict]:
    """Get all active (non-expired) facts for a session."""
    cypher = f"""
    MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
    WHERE f.valid_until IS NULL
    RETURN f ORDER BY f.source_turn DESC LIMIT $limit
    """
    results = await run_query(cypher, {"session_id": session_id, "limit": limit})
    facts = [r["f"] for r in results]
    return [_decode_fact(fact) for fact in facts]


async def get_active_fact_count(session_id: str) -> int:
    """Count active facts for a session (for monitoring)."""
    cypher = f"""
    MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
    WHERE f.valid_until IS NULL
    RETURN count(f) AS cnt
    """
    results = await run_query(cypher, {"session_id": session_id})
    return results[0]["cnt"] if results else 0


async def get_facts_filtered(
    session_id: str,
    fact_type: str | None = None,
    min_confidence: float | None = None,
    from_turn: int | None = None,
    to_turn: int | None = None,
    include_invalidated: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Get facts for a session with optional filtering.

    Args:
        session_id: The session to query.
        fact_type: Filter by fact type (observation, preference, procedure, etc.).
        min_confidence: Minimum confidence threshold (0.0-1.0).
        from_turn: Minimum source turn (inclusive).
        to_turn: Maximum source turn (inclusive).
        include_invalidated: Include facts that have been superseded (default False).
        limit: Max facts to return (default 100).
    """
    conditions = []
    if not include_invalidated:
        conditions.append("f.valid_until IS NULL")
    if fact_type:
        conditions.append("f.fact_type = $fact_type")
    if min_confidence is not None:
        conditions.append("f.confidence >= $min_confidence")
    if from_turn is not None:
        conditions.append("f.source_turn >= $from_turn")
    if to_turn is not None:
        conditions.append("f.source_turn <= $to_turn")

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    cypher = f"""
        MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
        {where_clause}
        RETURN f ORDER BY f.source_turn ASC LIMIT $limit
    """

    params: dict = {"session_id": session_id, "limit": limit}
    if fact_type:
        params["fact_type"] = fact_type
    if min_confidence is not None:
        params["min_confidence"] = min_confidence
    if from_turn is not None:
        params["from_turn"] = from_turn
    if to_turn is not None:
        params["to_turn"] = to_turn

    results = await run_query(cypher, params)
    return [_decode_fact(r["f"]) for r in results]


async def get_supersession_chain(session_id: str) -> list[dict]:
    """Get SUPERSEDES chains showing how facts evolved over a session.

    Returns facts that superseded others, grouped by which fact
    superseded them. This reveals how knowledge evolved over the session.
    """
    cypher = f"""
        MATCH (new:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
              -[:SUPERSEDES]->(old:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
        RETURN new.fact_id AS new_id, new.content AS new_content,
               new.source_turn AS new_turn, new.fact_type AS new_type,
               old.fact_id AS old_id, old.content AS old_content,
               old.source_turn AS old_turn, old.fact_type AS old_type
        ORDER BY new.source_turn ASC
    """
    results = await run_query(cypher, {"session_id": session_id})
    return [
        {
            "superseding_fact": {
                "fact_id": r["new_id"],
                "content": r["new_content"],
                "source_turn": r["new_turn"],
                "fact_type": r["new_type"],
            },
            "superseded_fact": {
                "fact_id": r["old_id"],
                "content": r["old_content"],
                "source_turn": r["old_turn"],
                "fact_type": r["old_type"],
            },
        }
        for r in results
    ]


async def get_invalidated_facts(session_id: str) -> list[dict]:
    """Get facts that have been invalidated (valid_until set) for a session."""
    cypher = f"""
        MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
        WHERE f.valid_until IS NOT NULL
        RETURN f.fact_id AS fact_id, f.content AS content,
               f.source_turn AS source_turn, f.fact_type AS fact_type,
               f.invalidated_at AS invalidated_at,
               f.source_tool AS source_tool, f.structured_json AS structured_json
        ORDER BY f.source_turn ASC
    """
    results = await run_query(cypher, {"session_id": session_id})
    return [
        {
            "fact_id": r["fact_id"],
            "content": r["content"],
            "source_turn": r["source_turn"],
            "fact_type": r["fact_type"],
            "invalidated_at": str(r["invalidated_at"]) if r.get("invalidated_at") else None,
            "source_tool": r.get("source_tool"),
            "structured": json.loads(r["structured_json"]) if r.get("structured_json") else None,
        }
        for r in results
    ]
