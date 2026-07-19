"""Durable snapshot persistence for the curator's in-memory caches (Phase 3, slice 1).

The curator keeps two process-local caches in ``curator/state.py``:
``_briefing_cache`` (session_id -> SessionBriefing) and ``_cache``
(session_id -> CuratorSnapshot). Both are lost on restart. This module persists
them to a stdlib ``sqlite3`` sidecar so a warm restart can reload them.

Design (see plan archolith-context-phase3-snapshot-durability-plan.md):
- In-memory dict write stays the primary, synchronous path. This module is a
  write-through mirror: ``state.py`` fires ``enqueue`` after updating its dict.
- Writes are drained by a single async consumer that batches and runs the
  sqlite work in a thread executor — no fsync on the hot path.
- The queue is bounded; on overflow we drop (best-effort durability — durability
  is a recovery-time optimization, not a correctness dependency).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import structlog

from archolith_proxy.curator.briefing import PreFetchedFile, SessionBriefing
from archolith_proxy.curator.state import CuratorSnapshot

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Serialization — standalone so the dataclasses stay untouched
# ---------------------------------------------------------------------------

def briefing_to_dict(b: SessionBriefing) -> dict:
    """Serialize a SessionBriefing to a JSON-safe dict."""
    return {
        "session_id": b.session_id,
        "source_turn": b.source_turn,
        "timestamp": b.timestamp,
        "checkpoint_text": b.checkpoint_text,
        "open_issues_text": b.open_issues_text,
        "last_verification_text": b.last_verification_text,
        "decisions_text": b.decisions_text,
        "session_goal": b.session_goal,
        "facts_text": b.facts_text,
        "files": [
            {
                "path": f.path,
                "outline": f.outline,
                "sections": [[s, e, c] for (s, e, c) in f.sections],
                "relevance": f.relevance,
            }
            for f in b.files
        ],
        "retained_turns": list(b.retained_turns) if b.retained_turns is not None else None,
        "turn_inventory": b.turn_inventory,
        "context_block": b.context_block,
        "mode": b.mode,
        "tool_calls_used": b.tool_calls_used,
        "iterations_used": b.iterations_used,
        "latency_ms": b.latency_ms,
    }


def briefing_from_dict(d: dict) -> SessionBriefing:
    """Reconstruct a SessionBriefing from its serialized dict."""
    files = [
        PreFetchedFile(
            path=f["path"],
            outline=f.get("outline", ""),
            sections=[(int(s), int(e), c) for (s, e, c) in f.get("sections", [])],
            relevance=f.get("relevance", ""),
        )
        for f in d.get("files", [])
    ]
    return SessionBriefing(
        session_id=d["session_id"],
        source_turn=d["source_turn"],
        timestamp=d.get("timestamp", 0.0),
        checkpoint_text=d.get("checkpoint_text", ""),
        open_issues_text=d.get("open_issues_text", ""),
        last_verification_text=d.get("last_verification_text", ""),
        decisions_text=d.get("decisions_text", ""),
        session_goal=d.get("session_goal", ""),
        facts_text=d.get("facts_text", ""),
        files=files,
        retained_turns=d.get("retained_turns"),
        turn_inventory=d.get("turn_inventory", ""),
        context_block=d.get("context_block", ""),
        mode=d.get("mode", "two_pass"),
        tool_calls_used=d.get("tool_calls_used", 0),
        iterations_used=d.get("iterations_used", 0),
        latency_ms=d.get("latency_ms", 0.0),
    )


def snapshot_to_dict(s: CuratorSnapshot) -> dict:
    """Serialize a CuratorSnapshot to a JSON-safe dict."""
    return {
        "curated_paths": list(s.curated_paths),
        "retained_turn_numbers": (
            list(s.retained_turn_numbers) if s.retained_turn_numbers is not None else None
        ),
        "context_summary": s.context_summary,
        "tool_calls_used": s.tool_calls_used,
        "turn_number": s.turn_number,
        "timestamp": s.timestamp,
    }


def snapshot_from_dict(d: dict) -> CuratorSnapshot:
    """Reconstruct a CuratorSnapshot from its serialized dict."""
    rtn = d.get("retained_turn_numbers")
    return CuratorSnapshot(
        curated_paths=tuple(d.get("curated_paths", [])),
        retained_turn_numbers=tuple(rtn) if rtn is not None else None,
        context_summary=d.get("context_summary", ""),
        tool_calls_used=d.get("tool_calls_used", 0),
        turn_number=d.get("turn_number", 0),
        timestamp=d.get("timestamp", 0.0),
    )


# ---------------------------------------------------------------------------
# Sidecar store with async write-through drain
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS curator_briefings (
    session_id TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    source_turn INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS curator_snapshots (
    session_id TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS context_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    rendered_block TEXT NOT NULL,
    files_selected_json TEXT,
    created_turn INTEGER,
    last_used_at REAL NOT NULL,
    is_cold_start INTEGER DEFAULT 0,
    UNIQUE(session_id, signature)
);
"""


class StatePersistence:
    """SQLite-backed durable mirror of the curator state caches."""

    def __init__(self, db_path: str, *, max_queue: int = 1000, batch_max: int = 64) -> None:
        self._db_path = db_path
        self._max_queue = max_queue
        self._batch_max = batch_max
        self._conn: sqlite3.Connection | None = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._dropped = 0

    async def start(self) -> None:
        """Open the db, ensure schema, and start the drain consumer."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._queue = asyncio.Queue(maxsize=self._max_queue)
        self._task = asyncio.create_task(self._drain())
        logger.info("curator_state_persistence_started", db=self._db_path)

    async def stop(self) -> None:
        """Flush pending writes, stop the consumer, close the db."""
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("curator_state_persistence_flush_timeout")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._dropped:
            logger.warning("curator_state_persistence_dropped", count=self._dropped)

    def enqueue(self, kind: str, session_id: str, obj) -> None:
        """Write-through hook called from state.py (sync, hot path).

        kind: "briefing" | "snapshot" | "delete". Never raises; drops on a full
        queue so the request path is never blocked or broken by persistence.
        """
        if self._queue is None:
            return
        try:
            self._queue.put_nowait((kind, session_id, obj))
        except asyncio.QueueFull:
            self._dropped += 1

    async def _drain(self) -> None:
        assert self._queue is not None
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            batch = [item]
            while len(batch) < self._batch_max:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await loop.run_in_executor(None, self._flush_batch, batch)
            except Exception:
                logger.warning("curator_state_persistence_flush_failed", exc_info=True)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _flush_batch(self, batch: list[tuple[str, str, object]]) -> None:
        """Apply a batch of write-through events (runs in a thread executor)."""
        conn = self._conn
        if conn is None:
            return
        import time as _time
        for kind, session_id, obj in batch:
            if kind == "briefing" and isinstance(obj, SessionBriefing):
                conn.execute(
                    "INSERT INTO curator_briefings (session_id, payload, source_turn, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "payload=excluded.payload, source_turn=excluded.source_turn, updated_at=excluded.updated_at",
                    (session_id, json.dumps(briefing_to_dict(obj)), obj.source_turn, _time.time()),
                )
            elif kind == "snapshot" and isinstance(obj, CuratorSnapshot):
                conn.execute(
                    "INSERT INTO curator_snapshots (session_id, payload, turn_number, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "payload=excluded.payload, turn_number=excluded.turn_number, updated_at=excluded.updated_at",
                    (session_id, json.dumps(snapshot_to_dict(obj)), obj.turn_number, _time.time()),
                )
            elif kind == "delete":
                conn.execute("DELETE FROM curator_briefings WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM curator_snapshots WHERE session_id = ?", (session_id,))
        conn.commit()

    async def load_all(self) -> tuple[dict[str, SessionBriefing], dict[str, CuratorSnapshot]]:
        """Read and reconstruct all persisted briefings and snapshots."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_all_sync)

    def _load_all_sync(self) -> tuple[dict[str, SessionBriefing], dict[str, CuratorSnapshot]]:
        conn = self._conn
        briefings: dict[str, SessionBriefing] = {}
        snapshots: dict[str, CuratorSnapshot] = {}
        if conn is None:
            return briefings, snapshots
        for sid, payload in conn.execute("SELECT session_id, payload FROM curator_briefings"):
            try:
                briefings[sid] = briefing_from_dict(json.loads(payload))
            except Exception:
                logger.warning("curator_state_briefing_load_failed", session_id=sid)
        for sid, payload in conn.execute("SELECT session_id, payload FROM curator_snapshots"):
            try:
                snapshots[sid] = snapshot_from_dict(json.loads(payload))
            except Exception:
                logger.warning("curator_state_snapshot_load_failed", session_id=sid)
        return briefings, snapshots


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_instance: StatePersistence | None = None


def get_state_persistence(db_path: str | None = None) -> StatePersistence:
    """Get (or lazily create) the process-wide StatePersistence singleton."""
    global _instance
    if _instance is None:
        if not db_path:
            db_path = "data/curator_state.db"
        _instance = StatePersistence(db_path)
    return _instance


def reset_state_persistence() -> None:
    """Drop the singleton (tests)."""
    global _instance
    _instance = None


__all__ = [
    "StatePersistence",
    "get_state_persistence",
    "reset_state_persistence",
    "briefing_to_dict",
    "briefing_from_dict",
    "snapshot_to_dict",
    "snapshot_from_dict",
]
