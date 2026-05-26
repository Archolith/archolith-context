"""LadybugDB graph backend — embedded columnar graph database (formerly Kuzu).

Implements the GraphBackend protocol using ladybug.Database + ladybug.AsyncConnection.
Uses explicit schema mode with native TIMESTAMP columns and current_timestamp() for
temporal operations. Interval arithmetic (current_timestamp() - s.last_active > to_hours(N))
powers TTL expiry without Python-side date math.

Cypher dialect notes:
- MERGE ON CREATE/ON MATCH SET → supported natively
- TIMESTAMP → native type, UTC-aware, ISO-8601 constructor
- current_timestamp() → server-side UTC timestamp
- to_hours($n) → creates INTERVAL from integer for temporal arithmetic
- DETACH DELETE and UNWIND → supported natively
"""

from __future__ import annotations

import atexit
import os
import time
from uuid import uuid4

import structlog

try:
    import ladybug

    _LADYBUG_AVAILABLE = True
except ImportError:
    _LADYBUG_AVAILABLE = False

from archolith_proxy.graph.protocol import GraphBackend

logger = structlog.get_logger()

_SCHEMA_DDL = """
CREATE NODE TABLE Session(
    session_id STRING PRIMARY KEY,
    fingerprint STRING,
    goal STRING,
    created_at TIMESTAMP,
    last_active TIMESTAMP,
    ttl_hours INT64,
    status STRING,
    turn_number INT64
);

CREATE NODE TABLE Fact(
    fact_id STRING PRIMARY KEY,
    session_id STRING,
    content STRING,
    fact_type STRING,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    invalidated_at TIMESTAMP,
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

CREATE NODE TABLE IF NOT EXISTS FileContent(
    file_id           STRING PRIMARY KEY,
    session_id        STRING,
    path              STRING,
    content           STRING,
    sha256            STRING,
    line_count        INT64,
    last_updated_turn INT64,
    created_at        TIMESTAMP
);

CREATE NODE TABLE IF NOT EXISTS FileOutline(
    outline_id        STRING PRIMARY KEY,
    session_id        STRING,
    path              STRING,
    outline           STRING,
    last_updated_turn INT64
);

CREATE NODE TABLE IF NOT EXISTS Checkpoint(
    session_id   STRING PRIMARY KEY,
    summary      STRING,
    next_step    STRING,
    confidence   DOUBLE,
    source_turn  INT64,
    updated_at   TIMESTAMP
);

CREATE NODE TABLE IF NOT EXISTS Issue(
    issue_id        STRING PRIMARY KEY,
    session_id      STRING,
    status          STRING,
    summary         STRING,
    related_file    STRING,
    related_command STRING,
    resolution_ref  STRING,
    source_turn     INT64,
    resolved_turn   INT64,
    created_at      TIMESTAMP
);

CREATE NODE TABLE IF NOT EXISTS Verification(
    verification_id STRING PRIMARY KEY,
    session_id      STRING,
    command         STRING,
    status          STRING,
    summary         STRING,
    source_turn     INT64,
    created_at      TIMESTAMP
);
"""


def _rotate_db_path(original_path: str) -> str:
    """Generate a fresh timestamped DB path alongside the original.

    Called when WAL recovery fails — produces a clean path guaranteed to have
    no corresponding .wal file so connect() can start fresh without recursion risk.

    Example: ./data/context.lbug → ./data/context_20260526_143022_recovered.lbug
    """
    base = original_path.removesuffix(".lbug") if original_path.endswith(".lbug") else original_path
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{base}_{timestamp}_recovered.lbug"


class LadybugBackend:
    """LadybugDB (embedded columnar graph) implementation of GraphBackend.

    Uses an in-process, file-backed database. No external server needed.
    Temporal columns use native TIMESTAMP type with current_timestamp() for
    writes and interval arithmetic for TTL expiry.

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
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)

        # Gap 1: WAL detection — log clearly when recovering from an unclean shutdown.
        wal_path = self._db_path + ".wal"
        wal_existed = os.path.exists(wal_path)
        if wal_existed:
            logger.warning(
                "ladybug_wal_detected",
                path=self._db_path,
                wal_path=wal_path,
                note="previous unclean shutdown detected — attempting WAL recovery",
            )

        # throw_on_wal_replay_failure=False: replay WAL up to last parseable transaction
        # rather than throwing and refusing to open.  Data loss is bounded to transactions
        # in-flight at kill time.  A clean SIGTERM shutdown still flushes fully via close().
        #
        # checkpoint_threshold=16MB: checkpoint aggressively to keep the WAL small and
        # reduce the transactions-in-flight window on kill.  Default (-1) defers too long.
        self._db = ladybug.Database(
            self._db_path,
            throw_on_wal_replay_failure=False,
            checkpoint_threshold=16 * 1024 * 1024,  # 16 MB
        )
        self._aconn = ladybug.AsyncConnection(
            self._db, max_concurrent_queries=self._max_concurrent
        )
        self._ready = True

        # Gap 4: atexit registration — belt-and-suspenders close if FastAPI lifespan
        # doesn't complete (e.g. startup exception after DB init).
        atexit.register(self._close_sync)

        # Create tables on first connect.
        await self.ensure_schema()

        # Startup health probe — confirm the DB is queryable after WAL replay.
        try:
            await self._aconn.execute("RETURN 1 AS ok")
            if wal_existed:
                logger.info("ladybug_wal_recovered", path=self._db_path)
            else:
                logger.info("ladybug_connected", path=self._db_path, max_concurrent=self._max_concurrent)
        except Exception as e:
            if wal_existed:
                # Gap 2: WAL replay succeeded but DB is still broken — auto-rotate to fresh path.
                logger.warning(
                    "ladybug_wal_recovery_failed",
                    path=self._db_path,
                    error=str(e),
                    note="rotating to fresh DB path",
                )
                await self.close()
                self._db_path = _rotate_db_path(self._db_path)
                logger.info("ladybug_rotating_to_fresh", new_path=self._db_path)
                await self.connect()  # recurse once with the fresh path (no WAL present)
            else:
                logger.warning(
                    "ladybug_connected_degraded",
                    path=self._db_path,
                    error=str(e),
                    note="health probe failed on clean DB — context may be unavailable",
                )

    async def close(self) -> None:
        self._close_sync()
        logger.info("ladybug_disconnected")

    def _close_sync(self) -> None:
        """Synchronous close — safe to call from atexit or signal handlers."""
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
                    # Convert native datetime/timedelta to ISO string for JSON compat
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
                goal: NULL, created_at: current_timestamp(),
                last_active: current_timestamp(),
                ttl_hours: 24, status: 'active', turn_number: 0
            }) RETURN s
            """,
            {"session_id": session_id, "fingerprint": fingerprint},
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

    async def find_or_create_by_fingerprint(
        self, fingerprint: str
    ) -> tuple[dict, bool]:
        """Atomically find or create a session by fingerprint (Ladybug variant)."""
        existing = await self.find_session_by_fingerprint(fingerprint)
        if existing:
            return existing, False
        session_id = uuid4().hex[:16]
        created = await self.create_session(session_id, fingerprint=fingerprint)
        if not created:
            # Race: another request created it between our check and create
            existing = await self.find_session_by_fingerprint(fingerprint)
            return existing or {}, False
        return created, True

    async def touch_session(self, session_id: str) -> None:
        await self._execute(
            """
            MATCH (s:Session {session_id: $session_id})
            SET s.last_active = current_timestamp(), s.turn_number = s.turn_number + 1
            """,
            {"session_id": session_id},
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
                valid_from: current_timestamp(), valid_until: NULL,
                invalidated_at: NULL, confidence: $confidence,
                source_turn: $source_turn, embedding: $embedding
            })
            """,
            {
                "fact_id": fact_id, "session_id": session_id,
                "content": content, "fact_type": fact_type,
                "confidence": confidence, "source_turn": source_turn,
                "embedding": embedding if embedding is not None else [],
            },
        )
        return fact_id

    async def store_facts_batch(self, session_id: str, facts: list[dict], source_turn: int) -> list[str]:
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
            })
        await self._execute(
            """
            UNWIND $params AS p
            CREATE (f:Fact {
                fact_id: p.fact_id, session_id: p.session_id,
                content: p.content, fact_type: p.fact_type,
                valid_from: current_timestamp(), valid_until: NULL,
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
            SET f.valid_until = current_timestamp(), f.invalidated_at = current_timestamp()
            RETURN count(f) AS invalidated
            """,
            {"fact_ids": fact_ids},
        )
        return rows[0]["invalidated"] if rows else 0

    async def find_matching_fact_ids(
        self, session_id: str, descriptions: list[str]
    ) -> list[str]:
        """Match description strings to actual fact IDs using Jaccard similarity."""
        if not descriptions:
            return []
        from archolith_proxy.extractor.dedup import jaccard_similarity

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
        # Read-then-write: File PK is synthetic (file_id), lookup is by natural key
        # (path+session_id). MERGE ON CREATE SET could provide file_id, but untested
        # with LadybugDB's explicit-schema PK requirements — kept as read-then-write.
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

    # ── File Content Cache ─────────────────────────────────────────────

    async def upsert_file_content(
        self, session_id: str, path: str, content: str, sha256: str, turn: int,
    ) -> None:
        """Store or update cached file content, using sha256 for dedup.

        If the existing row has the same sha256, skip the write (file unchanged).
        Otherwise update content, sha256, line_count, and last_updated_turn.
        """
        existing = await self._execute(
            "MATCH (fc:FileContent {session_id: $session_id, path: $path}) RETURN fc.sha256 AS sha256, fc.file_id AS file_id",
            {"session_id": session_id, "path": path},
        )
        if existing:
            existing_sha = existing[0].get("sha256")
            if existing_sha == sha256:
                logger.debug("file_cache_hit", path=path, session_id=session_id)
                return
            fid = existing[0].get("file_id")
            line_count = content.count("\n") + 1
            await self._execute(
                """
                MATCH (fc:FileContent {file_id: $fid})
                SET fc.content = $content, fc.sha256 = $sha256,
                    fc.line_count = $line_count, fc.last_updated_turn = $turn
                """,
                {"fid": fid, "content": content, "sha256": sha256,
                 "line_count": line_count, "turn": turn},
            )
            logger.debug("file_cache_updated", path=path, session_id=session_id)
        else:
            fid = "fc" + uuid4().hex[:14]
            line_count = content.count("\n") + 1
            await self._execute(
                """
                CREATE (fc:FileContent {
                    file_id: $fid, session_id: $session_id, path: $path,
                    content: $content, sha256: $sha256, line_count: $line_count,
                    last_updated_turn: $turn, created_at: current_timestamp()
                })
                """,
                {"fid": fid, "session_id": session_id, "path": path,
                 "content": content, "sha256": sha256,
                 "line_count": line_count, "turn": turn},
            )
            logger.debug("file_cache_created", path=path, session_id=session_id)

    async def get_file_content(self, session_id: str, path: str) -> dict | None:
        """Get cached file content. Returns {content, sha256, line_count} or None.

        Tries exact match first, then falls back to suffix match to handle
        absolute-vs-relative path mismatches (opencode stores absolute paths;
        agents may recall using relative paths).
        """
        rows = await self._execute(
            """
            MATCH (fc:FileContent {session_id: $session_id, path: $path})
            RETURN fc.content AS content, fc.sha256 AS sha256, fc.line_count AS line_count
            """,
            {"session_id": session_id, "path": path},
        )
        if rows:
            return rows[0]

        # Suffix fallback: normalize separators and check if any stored path ends
        # with the query path (handles absolute-stored vs relative-queried case).
        norm_query = path.replace("\\", "/").lstrip("/")
        if not norm_query:
            return None
        all_files = await self.list_cached_files(session_id)
        for f in all_files:
            stored = f.get("path", "").replace("\\", "/")
            if stored == norm_query or stored.endswith("/" + norm_query):
                full_rows = await self._execute(
                    """
                    MATCH (fc:FileContent {session_id: $session_id, path: $stored_path})
                    RETURN fc.content AS content, fc.sha256 AS sha256, fc.line_count AS line_count
                    """,
                    {"session_id": session_id, "stored_path": f["path"]},
                )
                return full_rows[0] if full_rows else None
        return None

    async def get_file_lines(self, session_id: str, path: str, start: int, end: int) -> str | None:
        """Retrieve a line range from cached file content (1-indexed, inclusive).

        Out-of-range end is clamped to EOF.
        """
        row = await self.get_file_content(session_id, path)
        if not row:
            return None
        lines = row["content"].split("\n")
        start = max(1, start)
        end = min(end, len(lines))
        if start > end:
            return None
        selected = lines[start - 1:end]
        numbered = [f"{start + i}: {line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    async def list_cached_files(self, session_id: str) -> list[dict]:
        """List all cached files. Returns [{path, sha256, line_count, last_updated_turn}]."""
        return await self._execute(
            """
            MATCH (fc:FileContent {session_id: $session_id})
            RETURN fc.path AS path, fc.sha256 AS sha256,
                   fc.line_count AS line_count, fc.last_updated_turn AS last_updated_turn
            ORDER BY fc.path ASC
            """,
            {"session_id": session_id},
        )

    async def delete_file_content(self, session_id: str, path: str) -> bool:
        """Delete a cached file entry. Returns True if a row was deleted."""
        rows = await self._execute(
            """
            MATCH (fc:FileContent {session_id: $session_id, path: $path})
            DELETE fc
            RETURN count(fc) AS deleted
            """,
            {"session_id": session_id, "path": path},
        )
        deleted = bool(rows and rows[0].get("deleted"))
        if deleted:
            logger.debug("file_cache_deleted", path=path, session_id=session_id)
        return deleted

    # ── File Outline Index ─────────────────────────────────────────────

    async def upsert_file_outline(
        self, session_id: str, path: str, outline: str, turn: int,
    ) -> None:
        """Store or update the structural outline for a cached file.

        Called alongside upsert_file_content whenever a file is ingested.
        Outline is a newline-separated list of 'line N: def foo' entries.
        """
        existing = await self._execute(
            "MATCH (fo:FileOutline {session_id: $session_id, path: $path}) RETURN fo.outline_id AS oid",
            {"session_id": session_id, "path": path},
        )
        if existing:
            oid = existing[0].get("oid")
            await self._execute(
                """
                MATCH (fo:FileOutline {outline_id: $oid})
                SET fo.outline = $outline, fo.last_updated_turn = $turn
                """,
                {"oid": oid, "outline": outline, "turn": turn},
            )
        else:
            oid = "fo" + uuid4().hex[:14]
            await self._execute(
                """
                CREATE (fo:FileOutline {
                    outline_id: $oid, session_id: $sid, path: $path,
                    outline: $outline, last_updated_turn: $turn
                })
                """,
                {"oid": oid, "sid": session_id, "path": path,
                 "outline": outline, "turn": turn},
            )
        logger.debug("file_outline_upserted", path=path, session_id=session_id)

    async def get_file_outline(self, session_id: str, path: str) -> str | None:
        """Retrieve the structural outline for a cached file, or None if unavailable.

        Tries exact path match first, then suffix fallback (same logic as
        get_file_content) to handle absolute-vs-relative path mismatches.
        """
        rows = await self._execute(
            "MATCH (fo:FileOutline {session_id: $session_id, path: $path}) RETURN fo.outline AS outline",
            {"session_id": session_id, "path": path},
        )
        if rows:
            return rows[0].get("outline") or None

        # Suffix fallback
        norm_query = path.replace("\\", "/").lstrip("/")
        if not norm_query:
            return None
        all_files = await self.list_cached_files(session_id)
        for f in all_files:
            stored = f.get("path", "").replace("\\", "/").lstrip("/")
            if stored.endswith(norm_query) or norm_query.endswith(stored):
                rows2 = await self._execute(
                    "MATCH (fo:FileOutline {session_id: $session_id, path: $path}) RETURN fo.outline AS outline",
                    {"session_id": session_id, "path": f.get("path", "")},
                )
                if rows2:
                    return rows2[0].get("outline") or None
        return None

    # ── Checkpoint ─────────────────────────────────────────────────────

    async def upsert_checkpoint(
        self, session_id: str, summary: str, next_step: str, confidence: float, turn: int,
    ) -> None:
        """Insert or overwrite the single checkpoint record for a session."""
        existing = await self._execute(
            "MATCH (c:Checkpoint {session_id: $session_id}) RETURN c.session_id AS sid",
            {"session_id": session_id},
        )
        if existing:
            await self._execute(
                """
                MATCH (c:Checkpoint {session_id: $session_id})
                SET c.summary = $summary, c.next_step = $next_step,
                    c.confidence = $confidence, c.source_turn = $turn,
                    c.updated_at = current_timestamp()
                """,
                {"session_id": session_id, "summary": summary, "next_step": next_step,
                 "confidence": confidence, "turn": turn},
            )
        else:
            await self._execute(
                """
                CREATE (c:Checkpoint {
                    session_id: $session_id, summary: $summary, next_step: $next_step,
                    confidence: $confidence, source_turn: $turn,
                    updated_at: current_timestamp()
                })
                """,
                {"session_id": session_id, "summary": summary, "next_step": next_step,
                 "confidence": confidence, "turn": turn},
            )

    async def get_checkpoint(self, session_id: str) -> dict | None:
        """Get the current checkpoint for a session. Returns None if none recorded."""
        rows = await self._execute(
            """
            MATCH (c:Checkpoint {session_id: $session_id})
            RETURN c.summary AS summary, c.next_step AS next_step,
                   c.confidence AS confidence, c.source_turn AS source_turn
            """,
            {"session_id": session_id},
        )
        return rows[0] if rows else None

    # ── Issues ─────────────────────────────────────────────────────────

    async def create_issue(
        self, session_id: str, summary: str, status: str,
        related_file: str, related_command: str, turn: int,
    ) -> None:
        """Record a new issue (open or resolved) for a session."""
        iid = "iss" + uuid4().hex[:13]
        await self._execute(
            """
            CREATE (i:Issue {
                issue_id: $iid, session_id: $session_id, status: $status,
                summary: $summary, related_file: $related_file,
                related_command: $related_command, resolution_ref: '',
                source_turn: $turn, resolved_turn: 0,
                created_at: current_timestamp()
            })
            """,
            {"iid": iid, "session_id": session_id, "status": status,
             "summary": summary, "related_file": related_file or "",
             "related_command": related_command or "", "turn": turn},
        )

    async def resolve_issues(
        self, session_id: str, summaries: list[str], resolution_ref: str, turn: int,
    ) -> None:
        """Mark open issues whose summary matches any entry in `summaries` as resolved."""
        for summary in summaries:
            await self._execute(
                """
                MATCH (i:Issue {session_id: $session_id, status: 'open', summary: $summary})
                SET i.status = 'resolved', i.resolution_ref = $ref, i.resolved_turn = $turn
                """,
                {"session_id": session_id, "summary": summary,
                 "ref": resolution_ref, "turn": turn},
            )

    async def get_open_issues(self, session_id: str) -> list[dict]:
        """Get all open issues for a session ordered by turn."""
        return await self._execute(
            """
            MATCH (i:Issue {session_id: $session_id, status: 'open'})
            RETURN i.issue_id AS issue_id, i.summary AS summary,
                   i.related_file AS related_file,
                   i.related_command AS related_command,
                   i.source_turn AS source_turn
            ORDER BY i.source_turn ASC
            """,
            {"session_id": session_id},
        )

    # ── Verifications ──────────────────────────────────────────────────

    async def create_verification(
        self, session_id: str, command: str, status: str, summary: str, turn: int,
    ) -> None:
        """Record a verification result (pass/fail/partial) for a session."""
        vid = "ver" + uuid4().hex[:13]
        await self._execute(
            """
            CREATE (v:Verification {
                verification_id: $vid, session_id: $session_id,
                command: $command, status: $status, summary: $summary,
                source_turn: $turn, created_at: current_timestamp()
            })
            """,
            {"vid": vid, "session_id": session_id, "command": command,
             "status": status, "summary": summary, "turn": turn},
        )

    async def get_last_verification(self, session_id: str) -> dict | None:
        """Get the most recent verification for a session."""
        rows = await self._execute(
            """
            MATCH (v:Verification {session_id: $session_id})
            RETURN v.command AS command, v.status AS status,
                   v.summary AS summary, v.source_turn AS source_turn
            ORDER BY v.source_turn DESC
            LIMIT 1
            """,
            {"session_id": session_id},
        )
        return rows[0] if rows else None

    # ── Cleanup / TTL ──────────────────────────────────────────────────

    async def expire_sessions(self) -> int:
        """Mark active sessions past their TTL as expired.

        Uses native TIMESTAMP arithmetic: current_timestamp() - s.last_active
        produces an INTERVAL, compared against to_hours(ttl_hours).
        """
        from archolith_proxy.config import get_settings
        settings = get_settings()
        rows = await self._execute(
            """
            MATCH (s:Session {status: 'active'})
            WHERE current_timestamp() - s.last_active > to_hours($ttl_hours)
            SET s.status = 'expired'
            RETURN count(s) AS expired
            """,
            {"ttl_hours": settings.session_ttl_hours},
        )
        count = rows[0]["expired"] if rows else 0
        if count:
            logger.info("ladybug_sessions_expired", count=count, ttl_hours=settings.session_ttl_hours)
        return count

    async def delete_expired_sessions(self) -> int:
        rows = await self._execute(
            """
            MATCH (s:Session {status: 'expired'})
            WITH s MATCH (s)-[r*0..]-(n)
            WHERE n.session_id = s.session_id
            DETACH DELETE n
            RETURN count(DISTINCT s) AS deleted
            """
        )
        count = rows[0]["deleted"] if rows else 0
        if count:
            logger.info("ladybug_expired_sessions_deleted", count=count)
        return count
