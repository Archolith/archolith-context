"""LadybugDB graph backend — embedded columnar graph database.

Implements the GraphBackend protocol using ladybug.Database + ladybug.AsyncConnection.
Domain logic is delegated to sub-modules (ladybug_sessions, ladybug_facts, etc.).
"""

from __future__ import annotations

__all__ = ["LadybugBackend"]

import atexit
import os
import time

import structlog

try:
    import ladybug

    _LADYBUG_AVAILABLE = True
except ImportError:
    _LADYBUG_AVAILABLE = False

from archolith_proxy.graph.ladybug_checkpoint import (
    bulk_create_issues,
    bulk_create_verifications,
    bulk_resolve_issues,
    create_issue,
    create_verification,
    get_checkpoint,
    get_last_verification,
    get_open_issues,
    resolve_issues,
    upsert_checkpoint,
)
from archolith_proxy.graph.ladybug_edges import (
    bulk_create_supersedes,
    bulk_create_touches,
    bulk_store_decisions,
    create_belongs_to,
    create_supersedes,
    create_touches,
    get_decisions,
    get_touched_files,
    store_decision,
)
from archolith_proxy.graph.ladybug_facts import (
    find_matching_fact_ids,
    get_active_fact_count,
    get_active_facts,
    get_facts_filtered,
    get_invalidated_facts,
    get_supersession_chain,
    invalidate_facts,
    store_fact,
    store_facts_batch,
)
from archolith_proxy.graph.ladybug_files import (
    delete_file_content,
    delete_file_outline,
    evict_stale_file_cache,
    get_file_content,
    get_file_lines,
    get_file_outline,
    list_cached_files,
    upsert_file_content,
    upsert_file_outline,
)
from archolith_proxy.graph.ladybug_sessions import (
    create_session,
    find_or_create_by_fingerprint,
    find_session_by_fingerprint,
    find_session_by_id,
    get_session_stats,
    get_turn_number,
    list_active_sessions,
    touch_session,
    update_goal,
)
from archolith_proxy.config import get_settings

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
    last_updated_turn INT64,
    created_at        TIMESTAMP
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
    base = original_path.removesuffix(".lbug") if original_path.endswith(".lbug") else original_path
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{base}_{timestamp}_recovered.lbug"


class LadybugBackend:
    """LadybugDB implementation of GraphBackend.

    Delegates domain operations to sub-modules (ladybug_sessions, ladybug_facts, etc.).
    """

    def __init__(self, db_path: str = "./data/context.lbug", max_concurrent_queries: int = 8):
        if not _LADYBUG_AVAILABLE:
            raise ImportError("LadybugDB not installed. Install with: pip install ladybug")
        self._db_path = db_path
        self._max_concurrent = max_concurrent_queries
        self._db = None
        self._aconn = None
        self._ready = False
        self._rotation_depth = 0  # Track rotation attempts to prevent infinite recursion

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        wal_path = self._db_path + ".wal"
        wal_existed = os.path.exists(wal_path)
        if wal_existed:
            logger.warning("ladybug_wal_detected", path=self._db_path, wal_path=wal_path,
                           note="previous unclean shutdown detected — attempting WAL recovery")

        self._db = ladybug.Database(
            self._db_path,
            throw_on_wal_replay_failure=False,
            checkpoint_threshold=16 * 1024 * 1024,
        )
        self._aconn = ladybug.AsyncConnection(self._db, max_concurrent_queries=self._max_concurrent)
        self._ready = True
        atexit.register(self._close_sync)
        await self.ensure_schema()

        try:
            await self._aconn.execute("RETURN 1 AS ok")
            self._rotation_depth = 0  # Reset on successful connection
            if wal_existed:
                logger.info("ladybug_wal_recovered", path=self._db_path)
            else:
                logger.info("ladybug_connected", path=self._db_path, max_concurrent=self._max_concurrent)
        except Exception as e:
            if wal_existed:
                logger.warning("ladybug_wal_recovery_failed", path=self._db_path, error=str(e),
                               note="rotating to fresh DB path")
                self._rotation_depth += 1
                if self._rotation_depth > 3:
                    logger.error("ladybug_rotation_depth_exceeded", path=self._db_path, depth=self._rotation_depth,
                                 error=str(e), note="giving up after 3 rotation attempts")
                    self._rotation_depth = 0
                    raise RuntimeError(f"LadybugDB connection failed after {self._rotation_depth} rotation attempts: {e}")
                await self.close()
                self._db_path = _rotate_db_path(self._db_path)
                logger.info("ladybug_rotating_to_fresh", new_path=self._db_path, attempt=self._rotation_depth)
                await self.connect()
            else:
                logger.warning("ladybug_connected_degraded", path=self._db_path, error=str(e),
                               note="health probe failed on clean DB")

    async def close(self) -> None:
        self._close_sync()
        logger.info("ladybug_disconnected")

    def _close_sync(self) -> None:
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

    def supported_methods(self) -> set[str]:
        """Return set of supported method names for LadybugDB backend.

        LadybugDB supports all operations including bulk operations and file caching.
        """
        return {
            # Lifecycle
            "connect", "close", "ensure_schema", "verify_connectivity", "is_ready",
            "supported_methods",
            # Session CRUD
            "create_session", "find_session_by_id", "find_session_by_fingerprint",
            "find_or_create_by_fingerprint", "touch_session", "get_turn_number",
            "update_goal", "list_active_sessions", "get_session_stats",
            # Fact CRUD
            "store_fact", "store_facts_batch", "invalidate_facts",
            "find_matching_fact_ids", "get_active_facts", "get_active_fact_count",
            "get_facts_filtered", "get_invalidated_facts", "get_supersession_chain",
            # All edge operations (single and bulk)
            "create_belongs_to", "create_touches", "bulk_create_touches",
            "create_supersedes", "bulk_create_supersedes",
            "get_touched_files", "store_decision", "bulk_store_decisions",
            "get_decisions",
            # File content caching (LadybugDB only)
            "upsert_file_content", "get_file_content", "delete_file_content",
            "list_cached_files", "get_file_lines",
            # File outline caching (LadybugDB only)
            "upsert_file_outline", "get_file_outline", "delete_file_outline",
            "evict_stale_file_cache",
            # Checkpoints, Issues, Verifications (LadybugDB only)
            "upsert_checkpoint", "get_checkpoint",
            "create_issue", "get_open_issues", "bulk_create_issues",
            "resolve_issues", "bulk_resolve_issues",
            "create_verification", "get_last_verification", "bulk_create_verifications",
        }

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

    # ── Session CRUD (delegated) ───────────────────────────────────────

    async def create_session(self, session_id: str, fingerprint: str | None = None) -> dict:
        return await create_session(self._execute, session_id, fingerprint)

    async def find_session_by_id(self, session_id: str) -> dict | None:
        return await find_session_by_id(self._execute, session_id)

    async def find_session_by_fingerprint(self, fingerprint: str) -> dict | None:
        return await find_session_by_fingerprint(self._execute, fingerprint)

    async def find_or_create_by_fingerprint(self, fingerprint: str) -> tuple[dict, bool]:
        return await find_or_create_by_fingerprint(self._execute, fingerprint)

    async def touch_session(self, session_id: str) -> None:
        return await touch_session(self._execute, session_id)

    async def get_turn_number(self, session_id: str) -> int:
        return await get_turn_number(self._execute, session_id)

    async def update_goal(self, session_id: str, goal: str) -> None:
        return await update_goal(self._execute, session_id, goal)

    async def list_active_sessions(self) -> list[dict]:
        return await list_active_sessions(self._execute)

    async def get_session_stats(self, session_id: str) -> dict:
        return await get_session_stats(self._execute, session_id)

    # ── Fact CRUD (delegated) ──────────────────────────────────────────

    async def store_fact(self, session_id: str, content: str, fact_type: str,
                         source_turn: int, confidence: float = 0.5,
                         embedding: list[float] | None = None) -> str:
        return await store_fact(self._execute, session_id, content, fact_type, source_turn, confidence, embedding)

    async def store_facts_batch(self, session_id: str, facts: list[dict], source_turn: int) -> list[str]:
        return await store_facts_batch(self._execute, session_id, facts, source_turn)

    async def invalidate_facts(self, fact_ids: list[str]) -> int:
        return await invalidate_facts(self._execute, fact_ids)

    async def find_matching_fact_ids(self, session_id: str, descriptions: list[str]) -> list[str]:
        return await find_matching_fact_ids(self._execute, session_id, descriptions)

    async def get_active_facts(self, session_id: str, limit: int = 50) -> list[dict]:
        return await get_active_facts(self._execute, session_id, limit)

    async def get_active_fact_count(self, session_id: str) -> int:
        return await get_active_fact_count(self._execute, session_id)

    async def get_facts_filtered(self, session_id: str, fact_type: str | None = None,
                                 min_confidence: float | None = None, from_turn: int | None = None,
                                 to_turn: int | None = None, include_invalidated: bool = False,
                                 limit: int = 100) -> list[dict]:
        return await get_facts_filtered(self._execute, session_id, fact_type, min_confidence,
                                        from_turn, to_turn, include_invalidated, limit)

    async def get_invalidated_facts(self, session_id: str) -> list[dict]:
        return await get_invalidated_facts(self._execute, session_id)

    async def get_supersession_chain(self, session_id: str) -> list[dict]:
        return await get_supersession_chain(self._execute, session_id)

    # ── Edge & Decision Operations (delegated) ─────────────────────────

    async def create_belongs_to(self, session_id: str, fact_id: str) -> None:
        return await create_belongs_to(self._execute, session_id, fact_id)

    async def create_touches(self, session_id: str, file_path: str, status: str, turn: int) -> None:
        return await create_touches(self._execute, session_id, file_path, status, turn)

    async def bulk_create_touches(self, session_id: str, touches: list[dict]) -> None:
        return await bulk_create_touches(self._execute, session_id, touches)

    async def create_supersedes(self, old_fact_id: str, new_fact_id: str) -> None:
        return await create_supersedes(self._execute, old_fact_id, new_fact_id)

    async def bulk_create_supersedes(self, pairs: list[tuple[str, str]]) -> None:
        return await bulk_create_supersedes(self._execute, pairs)

    async def get_touched_files(self, session_id: str) -> list[dict]:
        return await get_touched_files(self._execute, session_id)

    async def store_decision(self, session_id: str, summary: str, rationale: str | None, turn: int) -> str:
        return await store_decision(self._execute, session_id, summary, rationale, turn)

    async def bulk_store_decisions(self, session_id: str, decisions: list[dict], turn: int) -> list[str]:
        return await bulk_store_decisions(self._execute, session_id, decisions, turn)

    async def get_decisions(self, session_id: str, include_superseded: bool = False) -> list[dict]:
        return await get_decisions(self._execute, session_id, include_superseded)

    # ── File Content Cache (delegated) ─────────────────────────────────

    async def upsert_file_content(self, session_id: str, path: str, content: str, sha256: str, turn: int) -> None:
        return await upsert_file_content(self._execute, session_id, path, content, sha256, turn)

    async def get_file_content(self, session_id: str, path: str) -> dict | None:
        return await get_file_content(self._execute, session_id, path)

    async def get_file_lines(self, session_id: str, path: str, start: int, end: int) -> str | None:
        return await get_file_lines(self._execute, session_id, path, start, end)

    async def list_cached_files(self, session_id: str) -> list[dict]:
        return await list_cached_files(self._execute, session_id)

    async def delete_file_content(self, session_id: str, path: str) -> bool:
        return await delete_file_content(self._execute, session_id, path)

    async def delete_file_outline(self, session_id: str, path: str) -> bool:
        return await delete_file_outline(self._execute, session_id, path)

    async def upsert_file_outline(self, session_id: str, path: str, outline: str, turn: int) -> None:
        return await upsert_file_outline(self._execute, session_id, path, outline, turn)

    async def get_file_outline(self, session_id: str, path: str) -> str | None:
        return await get_file_outline(self._execute, session_id, path)

    async def evict_stale_file_cache(self, session_id: str, max_turns_age: int, max_entries: int) -> None:
        return await evict_stale_file_cache(self._execute, session_id, max_turns_age, max_entries)

    # ── Checkpoint / Issues / Verifications (delegated) ────────────────

    async def upsert_checkpoint(self, session_id: str, summary: str, next_step: str, confidence: float, turn: int) -> None:
        return await upsert_checkpoint(self._execute, session_id, summary, next_step, confidence, turn)

    async def get_checkpoint(self, session_id: str) -> dict | None:
        return await get_checkpoint(self._execute, session_id)

    async def create_issue(self, session_id: str, summary: str, status: str,
                           related_file: str, related_command: str, turn: int) -> None:
        return await create_issue(self._execute, session_id, summary, status, related_file, related_command, turn)

    async def bulk_create_issues(self, session_id: str, issues: list[dict], turn: int) -> list[str]:
        return await bulk_create_issues(self._execute, session_id, issues, turn)

    async def resolve_issues(self, session_id: str, summaries: list[str], resolution_ref: str, turn: int) -> None:
        return await resolve_issues(self._execute, session_id, summaries, resolution_ref, turn)

    async def bulk_resolve_issues(self, session_id: str, summaries: list[str], resolution_ref: str, turn: int) -> None:
        return await bulk_resolve_issues(self._execute, session_id, summaries, resolution_ref, turn)

    async def get_open_issues(self, session_id: str) -> list[dict]:
        return await get_open_issues(self._execute, session_id)

    async def create_verification(self, session_id: str, command: str, status: str, summary: str, turn: int) -> None:
        return await create_verification(self._execute, session_id, command, status, summary, turn)

    async def bulk_create_verifications(self, session_id: str, verifications: list[dict], turn: int) -> list[str]:
        return await bulk_create_verifications(self._execute, session_id, verifications, turn)

    async def get_last_verification(self, session_id: str) -> dict | None:
        return await get_last_verification(self._execute, session_id)

    # ── Cleanup / TTL ──────────────────────────────────────────────────

    async def expire_sessions(self) -> int:
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
