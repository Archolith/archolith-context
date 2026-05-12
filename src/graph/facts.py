"""Fact CRUD + vector similarity queries."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import structlog

from src.extractor.dedup import jaccard_similarity, _normalize
from src.graph.repository import CONTEXT_SESSION_LABEL, run_query, run_write
from src.models.graph_nodes import FactType

logger = structlog.get_logger()

# Similarity threshold for matching invalidated descriptions to existing facts
_INVALIDATION_MATCH_THRESHOLD = 0.60


async def store_fact(
    session_id: str,
    content: str,
    fact_type: FactType,
    source_turn: int,
    confidence: float = 0.5,
    embedding: list[float] | None = None,
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
        embedding: {embedding_prop}
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
    """Store multiple facts in a single transaction."""
    fact_ids = []
    for fact in facts:
        fid = await store_fact(
            session_id=session_id,
            content=fact.get("content", ""),
            fact_type=FactType(fact.get("fact_type", "observation")),
            source_turn=source_turn,
            confidence=fact.get("confidence", 0.5),
            embedding=fact.get("embedding"),
        )
        fact_ids.append(fid)
    return fact_ids


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

    active_facts = await get_active_facts(session_id, limit=200)
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
    return [r["f"] for r in results]


async def get_active_fact_count(session_id: str) -> int:
    """Count active facts for a session (for monitoring)."""
    cypher = f"""
    MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $session_id}})
    WHERE f.valid_until IS NULL
    RETURN count(f) AS cnt
    """
    results = await run_query(cypher, {"session_id": session_id})
    return results[0]["cnt"] if results else 0
