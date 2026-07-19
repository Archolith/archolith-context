"""LadybugDB fact CRUD operations."""

from __future__ import annotations

import base64
import json
from uuid import uuid4

from archolith_proxy.config import get_settings


_STRUCTURED_PREFIX = "b64:"


def _encode_structured(structured: dict | None) -> str | None:
    """Encode JSON for LadybugDB's content-sniffing STRING binder."""
    if not structured:
        return None
    payload = json.dumps(structured, separators=(",", ":"))
    return _STRUCTURED_PREFIX + base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_structured(raw: str | None) -> dict | None:
    """Decode current encoded and legacy plain structured payloads safely."""
    if not raw:
        return None
    try:
        if raw.startswith(_STRUCTURED_PREFIX):
            raw = base64.b64decode(raw[len(_STRUCTURED_PREFIX):].encode("ascii")).decode("utf-8")
        decoded = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _decode_fact(fact: dict) -> dict:
    raw = fact.get("structured_json")
    fact["structured"] = _decode_structured(raw)
    fact.pop("structured_json", None)
    return fact


async def store_fact(execute, session_id: str, content: str, fact_type: str,
                     source_turn: int, confidence: float = 0.5,
                     embedding: list[float] | None = None,
                     source_tool: str | None = None,
                     structured: dict | None = None) -> str:
    fact_id = "f" + uuid4().hex[:15]
    await execute(
        """
        CREATE (f:Fact {
            fact_id: $fact_id, session_id: $session_id,
            content: $content, fact_type: $fact_type,
            valid_from: current_timestamp(), valid_until: NULL,
            invalidated_at: NULL, confidence: $confidence,
            source_turn: $source_turn, embedding: $embedding,
            source_tool: $source_tool, structured_json: $structured_json
        })
        """,
        {"fact_id": fact_id, "session_id": session_id, "content": content,
         "fact_type": fact_type, "confidence": confidence,
         "source_turn": source_turn, "embedding": embedding if embedding is not None else None,
         "source_tool": source_tool,
         "structured_json": _encode_structured(structured)},
    )
    return fact_id


async def store_facts_batch(execute, session_id: str, facts: list[dict], source_turn: int) -> list[str]:
    if not facts:
        return []
    fact_ids = []
    params_list = []
    for fact in facts:
        fid = "f" + uuid4().hex[:15]
        fact_ids.append(fid)
        params_list.append({
            "fact_id": fid, "session_id": session_id,
            "content": fact.get("content", ""),
            "fact_type": fact.get("fact_type", "observation"),
            "confidence": fact.get("confidence", 0.5),
            "embedding": fact.get("embedding") or [],
            "source_turn": source_turn,
            "source_tool": fact.get("source_tool"),
            "structured_json": _encode_structured(fact.get("structured")),
        })
    await execute(
        """
        UNWIND $params AS p
        CREATE (f:Fact {
            fact_id: p.fact_id, session_id: p.session_id,
            content: p.content, fact_type: p.fact_type,
            valid_from: current_timestamp(), valid_until: NULL,
            invalidated_at: NULL, confidence: p.confidence,
            source_turn: p.source_turn, embedding: p.embedding,
            source_tool: p.source_tool, structured_json: p.structured_json
        })
        """,
        {"params": params_list},
    )
    return fact_ids


async def invalidate_facts(execute, fact_ids: list[str]) -> int:
    if not fact_ids:
        return 0
    rows = await execute(
        """
        MATCH (f:Fact) WHERE f.fact_id IN $fact_ids AND f.valid_until IS NULL
        SET f.valid_until = current_timestamp(), f.invalidated_at = current_timestamp()
        RETURN count(f) AS invalidated
        """,
        {"fact_ids": fact_ids},
    )
    return rows[0]["invalidated"] if rows else 0


async def find_matching_fact_ids(execute, session_id: str, descriptions: list[str]) -> list[str]:
    if not descriptions:
        return []
    from archolith_proxy.shared.text_utils import jaccard_similarity

    active_facts = await get_active_facts(execute, session_id, limit=get_settings().fact_pool_limit)
    if not active_facts:
        return []

    matched_ids: set = set()
    for desc in descriptions:
        best_id = None
        best_sim: float = 0.0
        for fact in active_facts:
            content = fact.get("content", "")
            if not content:
                continue
            sim = jaccard_similarity(desc, content)
            if sim > best_sim and sim >= 0.60:
                best_sim = sim
                best_id = fact.get("fact_id")
        if best_id:
            matched_ids.add(best_id)
    return list(matched_ids)


async def get_active_facts(execute, session_id: str, limit: int = 50) -> list[dict]:
    rows = await execute(
        "MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NULL RETURN f ORDER BY f.source_turn DESC LIMIT $limit",
        {"session_id": session_id, "limit": limit},
    )
    return [_decode_fact(fact) for fact in rows]


async def get_active_fact_count(execute, session_id: str) -> int:
    rows = await execute(
        "MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NULL RETURN count(f) AS cnt",
        {"session_id": session_id},
    )
    return rows[0]["cnt"] if rows else 0


async def get_facts_filtered(execute, session_id: str, fact_type: str | None = None,
                             min_confidence: float | None = None, from_turn: int | None = None,
                             to_turn: int | None = None, include_invalidated: bool = False,
                             limit: int = 100) -> list[dict]:
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

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    params: dict = {"session_id": session_id, "limit": limit}
    if fact_type:
        params["fact_type"] = fact_type
    if min_confidence is not None:
        params["min_confidence"] = min_confidence
    if from_turn is not None:
        params["from_turn"] = from_turn
    if to_turn is not None:
        params["to_turn"] = to_turn

    rows = await execute(
        f"MATCH (f:Fact {{session_id: $session_id}}) {where_clause} RETURN f ORDER BY f.source_turn ASC LIMIT $limit",
        params,
    )
    return [_decode_fact(fact) for fact in rows]


async def get_invalidated_facts(execute, session_id: str) -> list[dict]:
    rows = await execute(
        """
        MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NOT NULL
        RETURN f.fact_id AS fact_id, f.content AS content,
               f.source_turn AS source_turn, f.fact_type AS fact_type,
               f.invalidated_at AS invalidated_at,
               f.source_tool AS source_tool,
               f.structured_json AS structured_json
        ORDER BY f.source_turn ASC
        """,
        {"session_id": session_id},
    )
    return [_decode_fact(fact) for fact in rows]


async def get_supersession_chain(execute, session_id: str) -> list[dict]:
    rows = await execute(
        """
        MATCH (new:Fact {session_id: $session_id})
              -[:SUPERSEDES]->(old:Fact {session_id: $session_id})
        RETURN new.fact_id AS new_id, new.content AS new_content,
               new.source_turn AS new_turn, new.fact_type AS new_type,
               old.fact_id AS old_id, old.content AS old_content,
               old.source_turn AS old_turn, old.fact_type AS old_type
        ORDER BY new.source_turn ASC
        """,
        {"session_id": session_id},
    )
    return [
        {
            "superseding_fact": {"fact_id": r["new_id"], "content": r["new_content"], "source_turn": r["new_turn"], "fact_type": r["new_type"]},
            "superseded_fact": {"fact_id": r["old_id"], "content": r["old_content"], "source_turn": r["old_turn"], "fact_type": r["old_type"]},
        }
        for r in rows
    ]
