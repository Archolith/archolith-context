"""In-memory turn trace store with session-level indexing and optional disk persistence.

A single TraceStore instance lives on app.state.trace_store. It collects
TurnTrace records as they are produced during proxy request handling and
provides query methods for the /trace/* endpoints.

Design choices:
- In-memory (process-level) — traces are ephemeral; they reset on restart.
- Bounded per session — oldest turns are evicted when a session exceeds
  max_turns_per_session (default 100) to prevent memory leaks from
  long-running or abandoned sessions.
- Indexed by session_id and turn_id for O(1) lookups.
- Session summary is computed on demand from the stored turns.
- Optional disk persistence: when trace_dir is set, each trace record is
  also appended to a per-session JSONL file under trace_dir/<session_id>.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path

import structlog

from archolith_proxy.models.dtos import SessionTraceSummary, TurnTrace

logger = structlog.get_logger()

# Default: keep at most this many turns per session
DEFAULT_MAX_TURNS_PER_SESSION = 100


class TraceStore:
    """Process-level in-memory store for turn traces.

    Thread-safe via asyncio.Lock. Each session gets its own ordered list
    of TurnTrace records, indexed for fast lookup by turn_id.
    """

    def __init__(
        self,
        max_turns_per_session: int = DEFAULT_MAX_TURNS_PER_SESSION,
        max_sessions: int = 1000,
        trace_dir: str | None = None,
    ) -> None:
        self._max_turns = max_turns_per_session
        self._max_sessions = max_sessions
        self._lock = asyncio.Lock()
        # session_id -> list[TurnTrace] (ordered by turn_number)
        self._by_session: dict[str, list[TurnTrace]] = defaultdict(list)
        # turn_id -> TurnTrace (global index for direct lookup)
        self._by_turn_id: dict[str, TurnTrace] = {}
        # Track insertion order for LRU eviction
        self._session_order: list[str] = []
        self._total_traces = 0
        # Per-session metadata (harness_env, etc.) — set once on session creation
        self._session_meta: dict[str, dict[str, object]] = {}
        # Optional disk persistence
        self._trace_dir = Path(trace_dir) if trace_dir else None
        if self._trace_dir:
            self._trace_dir.mkdir(parents=True, exist_ok=True)
            logger.info("trace_disk_persistence_enabled", dir=str(self._trace_dir))

    async def record(self, trace: TurnTrace) -> None:
        """Store a turn trace record.

        Evicts oldest turns if the session exceeds the per-session limit.
        If disk persistence is enabled, appends the trace as JSONL.
        """
        async with self._lock:
            session_id = trace.session_id or "__no_session__"
            turn_list = self._by_session[session_id]

            # Evict oldest turns if over per-session limit
            while len(turn_list) >= self._max_turns:
                oldest = turn_list.pop(0)
                self._by_turn_id.pop(oldest.turn_id, None)

            # Evict least-recently-active session if over global cap
            if session_id not in self._session_order:
                self._session_order.append(session_id)
            else:
                # Move to end (most-recently touched)
                self._session_order.remove(session_id)
                self._session_order.append(session_id)

            while len(self._by_session) > self._max_sessions:
                # Evict the least-recently-active session
                evict_sid = self._session_order[0]
                if evict_sid != session_id:
                    for t in self._by_session.pop(evict_sid, []):
                        self._by_turn_id.pop(t.turn_id, None)
                    self._session_order.pop(0)
                else:
                    break  # Can't evict the current session

            turn_list.append(trace)
            self._by_turn_id[trace.turn_id] = trace
            self._total_traces += 1

        # Disk persistence (outside lock — I/O should not block reads)
        if self._trace_dir:
            try:
                safe_session = session_id.replace("/", "_").replace("\\", "_")
                path = self._trace_dir / f"{safe_session}.jsonl"
                line = trace.model_dump_json() + "\n"
                # Write append — no lock needed, OS provides atomic appends for small writes
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception:
                logger.warning("trace_disk_write_failed", session_id=session_id, exc_info=True)

    def set_session_metadata(
        self, session_id: str, key: str, value: object,
    ) -> None:
        """Store per-session metadata (e.g. harness_env).

        Non-async — called from the request path before any awaits.
        """
        meta = self._session_meta.setdefault(session_id, {})
        meta[key] = value

    def get_session_metadata(
        self, session_id: str, key: str,
    ) -> object | None:
        """Retrieve per-session metadata by key."""
        return self._session_meta.get(session_id, {}).get(key)

    async def get_turn(self, turn_id: str) -> TurnTrace | None:
        """Look up a single turn by its turn_id."""
        async with self._lock:
            return self._by_turn_id.get(turn_id)

    async def get_session_turns(
        self, session_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[TurnTrace]:
        """Get turns for a session, ordered by turn_number ascending.

        Supports pagination via limit/offset.
        """
        async with self._lock:
            turns = self._by_session.get(session_id, [])
            return turns[offset : offset + limit]

    async def get_session_summary(self, session_id: str) -> SessionTraceSummary | None:
        """Compute an aggregated summary for a session from its traces."""
        async with self._lock:
            turns = self._by_session.get(session_id, [])
            if not turns:
                return None

            # Aggregate metrics
            total_input = 0
            total_savings = 0
            total_facts_stored = 0
            total_dups_skipped = 0
            total_invalidations = 0
            total_recalls = 0
            modes: dict[str, int] = defaultdict(int)
            first_at = None
            last_at = None

            rewritten_input = 0  # input tokens from turns that were actually rewritten

            for t in turns:
                total_input += t.input_tokens
                total_savings += t.savings_tokens
                total_facts_stored += t.facts_stored
                total_dups_skipped += t.duplicates_skipped
                total_invalidations += t.invalidations_attempted
                if t.recall_used:
                    total_recalls += 1
                modes[t.assembly_mode] += 1
                if first_at is None or t.created_at < first_at:
                    first_at = t.created_at
                if last_at is None or t.created_at > last_at:
                    last_at = t.created_at
                if t.assembly_mode in ("curator", "graph", "briefing", "briefing_stale", "agent_solo_compressed"):
                    rewritten_input += t.input_tokens

            # Overall ratio: savings across all traffic
            avg_savings = total_savings / total_input if total_input > 0 else 0.0
            # Rewritten-only ratio: how effective the curator is per-turn it touches
            rewritten_ratio = total_savings / rewritten_input if rewritten_input > 0 else 0.0

            return SessionTraceSummary(
                session_id=session_id,
                goal=None,  # Filled by the endpoint from graph if available
                turn_count=len(turns),
                first_turn_at=first_at,
                last_turn_at=last_at,
                total_input_tokens=total_input,
                total_savings_tokens=total_savings,
                avg_savings_ratio=round(avg_savings, 4),
                rewritten_savings_ratio=round(rewritten_ratio, 4),
                assembly_modes=dict(modes),
                total_facts_stored=total_facts_stored,
                total_duplicates_skipped=total_dups_skipped,
                total_invalidations_attempted=total_invalidations,
                total_recalls=total_recalls,
                max_user_turns=max((t.user_turn_count for t in turns), default=0),
                harness_env=self._session_meta.get(session_id, {}).get("harness_env", {}),
            )

    async def list_sessions(self) -> list[SessionTraceSummary]:
        """List summaries for all sessions that have trace records."""
        async with self._lock:
            summaries = []
            for session_id in list(self._by_session.keys()):
                turns = self._by_session[session_id]
                if not turns:
                    continue

                total_input = sum(t.input_tokens for t in turns)
                total_savings = sum(t.savings_tokens for t in turns)
                modes: dict[str, int] = defaultdict(int)
                first_at = min(t.created_at for t in turns)
                last_at = max(t.created_at for t in turns)

                for t in turns:
                    modes[t.assembly_mode] += 1

                _rewritten_modes = {"curator", "graph", "briefing", "briefing_stale", "agent_solo_compressed"}
                rewritten_input = sum(t.input_tokens for t in turns if t.assembly_mode in _rewritten_modes)
                avg_savings = total_savings / total_input if total_input > 0 else 0.0
                rewritten_ratio = total_savings / rewritten_input if rewritten_input > 0 else 0.0

                summaries.append(SessionTraceSummary(
                    session_id=session_id,
                    goal=None,
                    turn_count=len(turns),
                    first_turn_at=first_at,
                    last_turn_at=last_at,
                    total_input_tokens=total_input,
                    total_savings_tokens=total_savings,
                    avg_savings_ratio=round(avg_savings, 4),
                    rewritten_savings_ratio=round(rewritten_ratio, 4),
                    assembly_modes=dict(modes),
                    total_facts_stored=sum(t.facts_stored for t in turns),
                    total_duplicates_skipped=sum(t.duplicates_skipped for t in turns),
                    total_invalidations_attempted=sum(t.invalidations_attempted for t in turns),
                    total_recalls=sum(1 for t in turns if t.recall_used),
                    max_user_turns=max((t.user_turn_count for t in turns), default=0),
                    harness_env=self._session_meta.get(session_id, {}).get("harness_env", {}),
                ))
            return summaries

    async def load_from_disk(self) -> int:
        """Reload trace records from JSONL files in trace_dir.

        Called at startup to restore historical sessions. Returns the
        number of records loaded. Skips malformed lines gracefully.
        """
        if not self._trace_dir or not self._trace_dir.exists():
            return 0

        import json as _json

        loaded = 0
        skipped_files = 0
        for jsonl_path in sorted(self._trace_dir.glob("*.jsonl")):
            # Only load files named as hex session IDs — skip other JSONL
            # files in the same directory (e.g. curator_failures.jsonl).
            stem = jsonl_path.stem
            try:
                int(stem, 16)
            except ValueError:
                skipped_files += 1
                continue

            try:
                with open(jsonl_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            # Quick pre-check: real trace records always have
                            # "created_at" in the JSON.  Records without it
                            # (e.g. curator failure dumps) get time.time() as
                            # default, poisoning last_turn_at with startup time.
                            raw = _json.loads(line)
                            if "created_at" not in raw:
                                continue
                            trace = TurnTrace.model_validate(raw)
                            session_id = trace.session_id or "__no_session__"
                            self._by_session[session_id].append(trace)
                            self._by_turn_id[trace.turn_id] = trace
                            self._total_traces += 1
                            loaded += 1
                            if session_id not in self._session_order:
                                self._session_order.append(session_id)
                        except Exception:
                            continue  # Skip malformed lines
            except Exception:
                logger.warning("trace_disk_load_failed", path=str(jsonl_path), exc_info=True)

        if loaded:
            logger.info(
                "trace_disk_loaded",
                records=loaded,
                sessions=len(self._by_session),
                dir=str(self._trace_dir),
            )
        return loaded

    async def get_max_turn_number(self, session_id: str) -> int | None:
        """Return the highest turn_number seen for a session, or None if no traces."""
        async with self._lock:
            turns = self._by_session.get(session_id, [])
            if not turns:
                return None
            return max(t.turn_number for t in turns)

    @property
    def total_traces(self) -> int:
        return self._total_traces

    @property
    def session_count(self) -> int:
        return len(self._by_session)


# Module-level singleton
_instance: TraceStore | None = None


def get_trace_store() -> TraceStore:
    """Get the process-level TraceStore instance.

    On first call, reads trace_dir from settings. When set, traces are
    persisted as per-session JSONL files and survive proxy restarts.
    """
    global _instance
    if _instance is None:
        from archolith_proxy.config import get_settings
        settings = get_settings()
        _instance = TraceStore(trace_dir=settings.trace_dir or None)
    return _instance


def reset_trace_store() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    _instance = None
