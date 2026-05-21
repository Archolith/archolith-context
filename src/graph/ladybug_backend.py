"""LadybugDB graph backend — embedded columnar graph database (formerly Kuzu).

Implements the GraphBackend protocol using ladybug.Database + ladybug.AsyncConnection.
Uses explicit schema mode with STRING-typed timestamp columns to avoid LadybugDB's
Cypher TIMESTAMP cast limitations.

Cypher dialect differences handled:
- MERGE ON CREATE/MATCH SET with PK columns → read-then-write fallback
- TIMESTAMP → stored as ISO-8601 STRING (converted at application level)
- $name params → validated to work in MATCH/MERGE WHERE and property maps
- DETACH DELETE and UNWIND → supported natively
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import structlog

try:
    import ladybug

    _LADYBUG_AVAILABLE = True
except ImportError:
    _LADYBUG_AVAILABLE = False

from src.graph.protocol import GraphBackend

logger = structlog.get_logger()

_SCHEMA_DDL = """
CREATE NODE TABLE Session(
    session_id STRING PRIMARY KEY,
    fingerprint STRING,
    goal STRING,
    created_at STRING,
    last_active STRING,
    ttl_hours INT64,
    status STRING,
    turn_number INT64
);

CREATE NODE TABLE Fact(
    fact_id STRING PRIMARY KEY,
    session_id STRING,
    content STRING,
    fact_type STRING,
    valid_from STRING,
    valid_until STRING,
    invalidated_at STRING,
    confidence DOUBLE,
    source_turn INT64,
    embedding DOUBLE[]
);

CREATE NODE TABLE File(
    file_id STRING PRIMARY KEY,
    path STRING,
    session_id STRING,
    status STRING,
    last_modified_turn INT64,
    last_read_turn INT64
);

CREATE NODE TABLE Decision(
    decision_id STRING PRIMARY KEY,
    session_id STRING,
    summary STRING,
    rationale STRING,
    turn INT64,
    superseded_by STRING
);

CREATE REL TABLE BELONGS_TO(FROM Fact TO Session);
CREATE REL TABLE TOUCHES(FROM Session TO File);
CREATE REL TABLE SUPERSEDES(FROM Fact TO Fact);
CREATE REL TABLE DECIDED_IN(FROM Decision TO Session);
"""


class LadybugBackend:
    """LadybugDB (embedded columnar graph) implementation of GraphBackend.

    Uses an in-process, file-backed database. No external server needed.
    Timestamps stored as ISO-8601 STRING (LadybugDB TIMESTAMP casting
    requires special handling we avoid at this layer).

    Args:
        db_path: Path to the LadybugDB database file (e.g. './data/context.lbug').
        max_concurrent_queries: Max concurrent queries for AsyncConnection.
    """

    def __init__(
        self,
        db_path: str = "./data/context.lbug",
        max_concurrent_queries: int = 8,
    ):
        if not _LADYBUG_AVAILABLE:
            raise ImportError(
                "LadybugDB not installed. Install with: pip install ladybug"
            )
        self._db_path = db_path
        self._max_concurrent = max_concurrent_queries
        self._db = None
        self._aconn = None
        self._ready = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        import os
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = ladybug.Database(self._db_path)
        self._aconn = ladybug.AsyncConnection(
            self._db, max_concurrent_queries=self._max_concurrent
        )
        self._ready = True
        logger.info("ladybug_connected", path=self._db_path, max_concurrent=self._max_concurrent)

    async def close(self) -> None:
        if self._aconn:
            try:
                self._aconn.close()
            except Exception:
                pass
            self._aconn = None
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._ready = False
        logger.info("ladybug_disconnected")

    async def ensure_schema(self) -> None:
        if not self._aconn:
            await self.connect()
        for statement in _SCHEMA_DDL.strip().split(";"):
            stmt = statement.strip()
            if not stmt:
                continue
            try:
                await self._aconn.execute(stmt)
            except Exception as e:
                err = str(e).lower()
                if "already exists" not in err and "duplicate" not in err:
                    logger.warning("ladybug_schema_warning", statement=stmt[:80], error=str(e)[:120])
        logger.info("ladybug_schema_ensured")

    async def verify_connectivity(self) -> bool:
        if not self._aconn:
            return False
        try:
            await self._aconn.execute("RETURN 1 AS ok")
            return True
        except Exception:
            return False

    def is_ready(self) -> bool:
        return self._ready and self._db is not None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _check_ready(self) -> None:
        if not self._ready or not self._aconn:
            raise RuntimeError("LadybugDB backend not connected")

    async def _execute(self, cypher: str, params: dict | None = None):
        """Execute a query and collect all results."""
        self._check_ready()
        result = await self._aconn.execute(cypher, params or {})
        rows = []
        while result.has_next():
            rows.append(result.get_next())
        # Map to dicts using column names
        col_names = result.get_column_names()
        dict_rows = []
        for row in rows:
            d = {}
            for idx, name in enumerate(col_names):
                if idx < len(row):
                    val = row[idx]
                    if hasattr(val, "isoformat"):
                        val = val.isoformat()
                    d[name] = val
            # Unwrap single-column node results: RETURN s => {"s": {...}} => {...}
            if len(col_names) == 1 and len(d) == 1:
                sole_key = list(d.keys())[0]
                sole_val = d[sole_key]
                if isinstance(sole_val, dict):
                    dict_rows.append(sole_val)
                else:
                    dict_rows.append(d)
            else:
                dict_rows.append(d)
        return dict_rows

    # ── Session CRUD ───────────────────────────────────────────────────

    async def create_session(self, session_id: str, fingerprint: str | None = None) -> dict:
        rows = await self._execute(
            """
            CREATE (s:Session {
                session_id: $session_id, fingerprint: $fingerprint,
                goal: NULL, created_at: $now, last_active: $now,
                ttl_hours: 24, status: 'active', turn_number: 0
            }) RETURN s
            """,
            {"session_id": session_id, "fingerprint": fingerprint, "now": self._now()},
        )
        return rows[0] if rows else {}

    async def find_session_by_id(self, session_id: str) -> dict | None:
        rows = await self._execute(
            "MATCH (s:Session {session_id: $session_id}) RETURN s",
            {"session_id": session_id},
        )
        return rows[0] if rows else None

    async def find_session_by_fingerprint(self, fingerprint: str) -> dict | None:
        rows = await self._execute(
            "MATCH (s:Session {fingerprint: $fingerprint}) RETURN s",
            {"fingerprint": fingerprint},
        )
        return rows[0] if rows else None

    async def touch_session(self, session_id: str) -> None:
        await self._execute(
            """
            MATCH (s:Session {session_id: $session_id})
            SET s.last_active = $now, s.turn_number = s.turn_number + 1
            """,
            {"session_id": session_id, "now": self._now()},
        )

    async def get_turn_number(self, session_id: str) -> int:
        rows = await self._execute(
            "MATCH (s:Session {session_id: $session_id}) RETURN s.turn_number AS turn",
            {"session_id": session_id},
        )
        return rows[0]["turn"] if rows else 0

    async def update_goal(self, session_id: str, goal: str) -> None:
        await self._execute(
            "MATCH (s:Session {session_id: $session_id}) SET s.goal = $goal",
            {"session_id": session_id, "goal": goal},
        )

    async def list_active_sessions(self) -> list[dict]:
        return await self._execute(
            """
            MATCH (s:Session {status: 'active'})
            RETURN s.session_id AS session_id, s.fingerprint AS fingerprint,
                   s.turn_number AS turn_number, s.created_at AS created_at,
                   s.last_active AS last_active, s.goal AS goal
            ORDER BY s.last_active DESC
            """
        )

    async def get_session_stats(self, session_id: str) -> dict:
        facts = await self._execute(
            "MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NULL RETURN count(f) AS active_facts",
            {"session_id": session_id},
        )
        session = await self._execute(
            "MATCH (s:Session {session_id: $session_id}) RETURN s.turn_number AS turn_number, s.goal AS goal, s.status AS status, s.created_at AS created_at, s.last_active AS last_active",
            {"session_id": session_id},
        )
        if not session:
            return {}
        result = dict(session[0])
        result["active_facts"] = facts[0]["active_facts"] if facts else 0
        return result

    # ── Fact CRUD ──────────────────────────────────────────────────────

    async def store_fact(
        self, session_id: str, content: str, fact_type: str,
        source_turn: int, confidence: float = 0.5,
        embedding: list[float] | None = None,
    ) -> str:
        fact_id = "f" + uuid4().hex[:15]
        await self._execute(
            """
            CREATE (f:Fact {
                fact_id: $fact_id, session_id: $session_id,
                content: $content, fact_type: $fact_type,
                valid_from: $now, valid_until: NULL,
                invalidated_at: NULL, confidence: $confidence,
                source_turn: $source_turn, embedding: $embedding
            })
            """,
            {
                "fact_id": fact_id, "session_id": session_id,
                "content": content, "fact_type": fact_type,
                "now": self._now(), "confidence": confidence,
                "source_turn": source_turn,
                "embedding": embedding if embedding is not None else [],
            },
        )
        return fact_id

    async def store_facts_batch(self, session_id: str, facts: list[dict], source_turn: int) -> list[str]:
        if not facts:
            return []
        now = self._now()
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
                "now": now, "source_turn": source_turn,
            })
        await self._execute(
            """
            UNWIND $params AS p
            CREATE (f:Fact {
                fact_id: p.fact_id, session_id: p.session_id,
                content: p.content, fact_type: p.fact_type,
                valid_from: p.now, valid_until: NULL,
                invalidated_at: NULL, confidence: p.confidence,
                source_turn: p.source_turn, embedding: p.embedding
            })
            """,
            {"params": params_list},
        )
        return fact_ids

    async def invalidate_facts(self, fact_ids: list[str]) -> int:
        if not fact_ids:
            return 0
        rows = await self._execute(
            """
            MATCH (f:Fact) WHERE f.fact_id IN $fact_ids AND f.valid_until IS NULL
            SET f.valid_until = $now, f.invalidated_at = $now
            RETURN count(f) AS invalidated
            """,
            {"fact_ids": fact_ids, "now": self._now()},
        )
        return rows[0]["invalidated"] if rows else 0

    async def find_matching_fact_ids(
        self, session_id: str, descriptions: list[str]
    ) -> list[str]:
        """Match description strings to actual fact IDs using Jaccard similarity."""
        if not descriptions:
            return []
        from src.extractor.dedup import jaccard_similarity

        active_facts = await self.get_active_facts(session_id, limit=200)
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

    async def get_active_facts(self, session_id: str, limit: int = 50) -> list[dict]:
        rows = await self._execute(
            "MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NULL RETURN f ORDER BY f.source_turn DESC LIMIT $limit",
            {"session_id": session_id, "limit": limit},
        )
        return rows  # Unwrapped by _execute

    async def get_active_fact_count(self, session_id: str) -> int:
        rows = await self._execute(
            "MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NULL RETURN count(f) AS cnt",
            {"session_id": session_id},
        )
        return rows[0]["cnt"] if rows else 0

    async def get_facts_filtered(
        self, session_id: str, fact_type: str | None = None,
        min_confidence: float | None = None, from_turn: int | None = None,
        to_turn: int | None = None, include_invalidated: bool = False,
        limit: int = 100,
    ) -> list[dict]:
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

        rows = await self._execute(
            f"MATCH (f:Fact {{session_id: $session_id}}) {where_clause} RETURN f ORDER BY f.source_turn ASC LIMIT $limit",
            params,
        )
        return rows  # Unwrapped by _execute

    async def get_invalidated_facts(self, session_id: str) -> list[dict]:
        return await self._execute(
            """
            MATCH (f:Fact {session_id: $session_id}) WHERE f.valid_until IS NOT NULL
            RETURN f.fact_id AS fact_id, f.content AS content,
                   f.source_turn AS source_turn, f.fact_type AS fact_type,
                   f.invalidated_at AS invalidated_at
            ORDER BY f.source_turn ASC
            """,
            {"session_id": session_id},
        )

    async def get_supersession_chain(self, session_id: str) -> list[dict]:
        rows = await self._execute(
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

    # ── Edge Operations ────────────────────────────────────────────────

    async def create_belongs_to(self, session_id: str, fact_id: str) -> None:
        await self._execute(
            "MATCH (s:Session {session_id: $session_id}) MATCH (f:Fact {fact_id: $fact_id}) MERGE (f)-[:BELONGS_TO]->(s)",
            {"session_id": session_id, "fact_id": fact_id},
        )

    async def create_touches(self, session_id: str, file_path: str, status: str, turn: int) -> None:
        existing = await self._execute(
            "MATCH (f:File {path: $path, session_id: $session_id}) RETURN f.file_id",
            {"path": file_path, "session_id": session_id},
        )
        if existing:
            fid = existing[0]["f.file_id"]
            sets = []
            params = {"fid": fid}
            sets.append("f.status = $status")
            params["status"] = status
            if status in ("modified", "created", "deleted"):
                sets.append("f.last_modified_turn = $turn")
                params["turn"] = turn
            if status == "read":
                sets.append("f.last_read_turn = $turn")
                params["turn"] = turn
            await self._execute(
                f"MATCH (f:File {{file_id: $fid}}) SET {', '.join(sets)}", params,
            )
        else:
            fid = "fl" + uuid4().hex[:14]
            await self._execute(
                """
                CREATE (f:File {
                    file_id: $fid, path: $path, session_id: $session_id,
                    status: $status, last_modified_turn: $lmt, last_read_turn: $lrt
                })
                """,
                {
                    "fid": fid, "path": file_path, "session_id": session_id,
                    "status": status,
                    "lmt": turn if status in ("modified", "created", "deleted") else 0,
                    "lrt": turn if status == "read" else 0,
                },
            )
        await self._execute(
            "MATCH (s:Session {session_id: $session_id}) MATCH (f:File {path: $path, session_id: $session_id}) MERGE (s)-[:TOUCHES]->(f)",
            {"session_id": session_id, "path": file_path},
        )

    async def create_supersedes(self, old_fact_id: str, new_fact_id: str) -> None:
        await self._execute(
            "MATCH (old:Fact {fact_id: $old_id}) MATCH (new:Fact {fact_id: $new_id}) MERGE (new)-[:SUPERSEDES]->(old)",
            {"old_id": old_fact_id, "new_id": new_fact_id},
        )

    async def get_touched_files(self, session_id: str) -> list[dict]:
        return await self._execute(
            """
            MATCH (s:Session {session_id: $session_id})-[:TOUCHES]->(f:File)
            RETURN f.path AS path, f.status AS status,
                   f.last_modified_turn AS last_modified_turn,
                   f.last_read_turn AS last_read_turn
            ORDER BY f.last_modified_turn DESC, f.last_read_turn DESC
            """,
            {"session_id": session_id},
        )

    # ── Decision Operations ────────────────────────────────────────────

    async def store_decision(self, session_id: str, summary: str, rationale: str | None, turn: int) -> str:
        decision_id = "d" + uuid4().hex[:15]
        await self._execute(
            """
            CREATE (d:Decision {
                decision_id: $decision_id, session_id: $session_id,
                summary: $summary, rationale: $rationale,
                turn: $turn, superseded_by: NULL
            })
            """,
            {"decision_id": decision_id, "session_id": session_id, "summary": summary, "rationale": rationale, "turn": turn},
        )
        # Link Decision to Session via DECIDED_IN edge
        await self._execute(
            "MATCH (d:Decision {decision_id: $decision_id}) MATCH (s:Session {session_id: $session_id}) MERGE (d)-[:DECIDED_IN]->(s)",
            {"decision_id": decision_id, "session_id": session_id},
        )
        return decision_id

    async def get_decisions(self, session_id: str, include_superseded: bool = False) -> list[dict]:
        filter_clause = "" if include_superseded else "WHERE d.superseded_by IS NULL"
        return await self._execute(
            f"""
            MATCH (d:Decision {{session_id: $session_id}}) {filter_clause}
            RETURN d.decision_id AS decision_id, d.summary AS summary,
                   d.rationale AS rationale, d.turn AS turn,
                   d.superseded_by AS superseded_by
            ORDER BY d.turn ASC
            """,
            {"session_id": session_id},
        )

    # ── Cleanup / TTL ──────────────────────────────────────────────────

    async def expire_sessions(self) -> int:
        rows = await self._execute("MATCH (s:Session {status: 'active'}) RETURN count(s) AS expired")
        count = rows[0]["expired"] if rows else 0
        if count:
            logger.info("ladybug_sessions_expired", count=count)
        return count

    async def delete_expired_sessions(self) -> int:
        rows = await self._execute(
            """
            MATCH (s:Session {status: 'expired'})
            WITH s MATCH (s)-[r*0..]-(n)
            DETACH DELETE n
            RETURN count(DISTINCT s) AS deleted
            """
        )
        count = rows[0]["deleted"] if rows else 0
        if count:
            logger.info("ladybug_expired_sessions_deleted", count=count)
        return count
