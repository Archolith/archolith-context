"""Abstract graph database backend protocol.

Defines the full graph operation surface as a runtime-checkable typing.Protocol.
All graph modules (sessions, facts, edges, decisions, cleanup) operate through
this interface, enabling pluggable backends (Neo4j, LadybugDB, etc.).
"""

from __future__ import annotations

__all__ = ["GraphBackend"]

from typing import Protocol, runtime_checkable

from archolith_proxy.models.graph_nodes import FactType


@runtime_checkable
class GraphBackend(Protocol):
    """Abstract graph database backend.

    Implementations wrap a concrete graph database (Neo4j, LadybugDB, etc.)
    and provide the full session/fact/edge/decision/cleanup surface.
    """

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize the connection pool and verify connectivity."""
        ...

    async def close(self) -> None:
        """Close all connections and release resources."""
        ...

    async def ensure_schema(self) -> None:
        """Create required indexes and constraints.

        Safe to call on every startup (IF NOT EXISTS semantics).
        """
        ...

    async def verify_connectivity(self) -> bool:
        """Verify that the backend is reachable and responsive."""
        ...

    def is_ready(self) -> bool:
        """Return True if the backend is connected and ready."""
        ...

    def supported_methods(self) -> set[str]:
        """Return set of supported method names.

        Methods not in this set will raise NotImplementedError.
        Allows callers to check capability before calling. Example:
        if 'bulk_create_touches' in backend.supported_methods():
            ...
        """
        ...

    # ── Session CRUD ───────────────────────────────────────────────────

    async def create_session(
        self, session_id: str, fingerprint: str | None = None
    ) -> dict:
        """Create a new session node.

        Returns the raw node properties dict.
        """
        ...

    async def find_session_by_id(self, session_id: str) -> dict | None:
        """Look up a session by session_id. Returns None if not found."""
        ...

    async def find_session_by_fingerprint(self, fingerprint: str) -> dict | None:
        """Look up a session by fingerprint. Returns None if not found."""
        ...

    async def find_or_create_by_fingerprint(
        self, fingerprint: str
    ) -> tuple[dict, bool]:
        """Atomically find or create a session by fingerprint.

        Uses MERGE to avoid lookup-then-create races. Returns (session_data, is_new).
        """
        ...

    async def touch_session(self, session_id: str) -> None:
        """Update last_active and increment turn_number."""
        ...

    async def get_turn_number(self, session_id: str) -> int:
        """Get current turn number for a session. Returns 0 if no session."""
        ...

    async def update_goal(self, session_id: str, goal: str) -> None:
        """Update the session goal string."""
        ...

    async def set_session_config_overrides(self, session_id: str, overrides_json: str) -> None:
        """Persist a JSON string of per-session config overrides on the session."""
        ...

    async def get_session_config_overrides(self, session_id: str) -> str:
        """Return the per-session config overrides JSON string ('' if none)."""
        ...

    async def list_active_sessions(self) -> list[dict]:
        """List all active sessions (for admin/metrics endpoints)."""
        ...

    async def get_session_stats(self, session_id: str) -> dict:
        """Get stats for a specific session (turn count, active facts, etc.)."""
        ...

    # ── Fact CRUD ──────────────────────────────────────────────────────

    async def store_fact(
        self,
        session_id: str,
        content: str,
        fact_type: str | FactType,
        source_turn: int,
        confidence: float = 0.5,
        embedding: list[float] | None = None,
    ) -> str:
        """Store a single fact in the session graph.

        Returns the assigned fact_id.
        """
        ...

    async def store_facts_batch(
        self,
        session_id: str,
        facts: list[dict],
        source_turn: int,
    ) -> list[str]:
        """Store multiple facts in a single transaction (or batch).

        Returns list of assigned fact_ids.
        """
        ...

    async def invalidate_facts(self, fact_ids: list[str]) -> int:
        """Mark facts as invalidated (set valid_until). Returns count affected."""
        ...

    async def find_matching_fact_ids(
        self, session_id: str, descriptions: list[str]
    ) -> list[str]:
        """Match invalidation description strings to actual fact IDs.

        Uses Jaccard similarity to find the best match for each description
        among active facts in the session. Returns matched fact_id list.
        """
        ...

    async def get_active_facts(
        self, session_id: str, limit: int = 50
    ) -> list[dict]:
        """Get all active (non-expired) facts for a session."""
        ...

    async def get_active_fact_count(self, session_id: str) -> int:
        """Count active facts for a session (for monitoring)."""
        ...

    async def get_facts_filtered(
        self,
        session_id: str,
        fact_type: str | None = None,
        min_confidence: float | None = None,
        from_turn: int | None = None,
        to_turn: int | None = None,
        include_invalidated: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """Get facts for a session with optional filtering."""
        ...

    async def get_invalidated_facts(self, session_id: str) -> list[dict]:
        """Get facts that have been invalidated (valid_until set)."""
        ...

    async def get_supersession_chain(self, session_id: str) -> list[dict]:
        """Get SUPERSEDES chains showing how facts evolved over a session."""
        ...

    # ── Edge Operations ────────────────────────────────────────────────

    async def create_belongs_to(self, session_id: str, fact_id: str) -> None:
        """Link a fact to its session via BELONGS_TO edge."""
        ...

    async def create_touches(
        self, session_id: str, file_path: str, status: str, turn: int
    ) -> None:
        """Create or update a TOUCHES edge from session to file."""
        ...

    async def bulk_create_touches(
        self, session_id: str, touches: list[dict]
    ) -> None:
        """Batch create/update File nodes and TOUCHES edges (single UNWIND)."""
        ...

    async def create_supersedes(
        self, old_fact_id: str, new_fact_id: str
    ) -> None:
        """Link a new fact as superseding an old one."""
        ...

    async def bulk_create_supersedes(
        self, pairs: list[tuple[str, str]]
    ) -> None:
        """Batch create SUPERSEDES edges (single UNWIND)."""
        ...

    async def get_touched_files(self, session_id: str) -> list[dict]:
        """Get all files touched in a session."""
        ...

    # ── Decision Operations ────────────────────────────────────────────

    async def store_decision(
        self,
        session_id: str,
        summary: str,
        rationale: str | None,
        turn: int,
    ) -> str:
        """Store a decision node and link to session. Returns decision_id."""
        ...

    async def bulk_store_decisions(
        self, session_id: str, decisions: list[dict], turn: int
    ) -> list[str]:
        """Batch create Decision nodes (single UNWIND). Returns decision_ids."""
        ...

    async def get_decisions(
        self, session_id: str, include_superseded: bool = False
    ) -> list[dict]:
        """Get all decisions for a session."""
        ...

    # ── File Content Cache ─────────────────────────────────────────────

    async def upsert_file_content(
        self, session_id: str, path: str, content: str, sha256: str, turn: int,
    ) -> None:
        """Store or update cached file content. Uses sha256 for dedup."""
        ...

    async def get_file_content(self, session_id: str, path: str) -> dict | None:
        """Get cached file content. Returns {content, sha256, line_count} or None."""
        ...

    async def get_file_lines(
        self, session_id: str, path: str, start: int, end: int,
    ) -> str | None:
        """Retrieve a line range from cached file content (1-indexed, inclusive)."""
        ...

    async def upsert_file_outline(
        self, session_id: str, path: str, outline: str, turn: int,
    ) -> None:
        """Store or update a file's structural outline (line N: def foo entries)."""
        ...

    async def get_file_outline(self, session_id: str, path: str) -> str | None:
        """Get a file's structural outline, or None if not indexed."""
        ...

    async def list_cached_files(self, session_id: str) -> list[dict]:
        """List all cached files for a session."""
        ...

    async def delete_file_content(self, session_id: str, path: str) -> bool:
        """Delete a cached file entry. Returns True if a row was deleted."""
        ...

    async def delete_file_outline(self, session_id: str, path: str) -> bool:
        """Delete a file outline entry. Returns True if a row was deleted."""
        ...

    async def evict_stale_file_cache(
        self, session_id: str, max_turns_age: int, max_entries: int
    ) -> None:
        """Evict stale file cache entries based on TTL and max entry count.

        - Removes entries where last_updated_turn < (current_turn - max_turns_age)
        - If still over max_entries, evicts oldest entries by last_updated_turn
        """
        ...

    # ── Checkpoint ─────────────────────────────────────────────────────

    async def upsert_checkpoint(
        self, session_id: str, summary: str, next_step: str, confidence: float, turn: int,
    ) -> None:
        """Insert or overwrite the single checkpoint record for a session."""
        ...

    async def get_checkpoint(self, session_id: str) -> dict | None:
        """Get the current checkpoint. Returns None if none recorded."""
        ...

    # ── Issues ─────────────────────────────────────────────────────────

    async def create_issue(
        self, session_id: str, summary: str, status: str,
        related_file: str, related_command: str, turn: int,
    ) -> None:
        """Record a new issue for a session."""
        ...

    async def bulk_create_issues(
        self, session_id: str, issues: list[dict], turn: int
    ) -> list[str]:
        """Batch create Issue nodes (single UNWIND). Returns issue_ids."""
        ...

    async def resolve_issues(
        self, session_id: str, summaries: list[str], resolution_ref: str, turn: int,
    ) -> None:
        """Mark open issues matching summaries as resolved."""
        ...

    async def bulk_resolve_issues(
        self, session_id: str, summaries: list[str], resolution_ref: str, turn: int,
    ) -> None:
        """Batch resolve issues (single UNWIND)."""
        ...

    async def get_open_issues(self, session_id: str) -> list[dict]:
        """Get all open issues for a session."""
        ...

    # ── Verifications ──────────────────────────────────────────────────

    async def create_verification(
        self, session_id: str, command: str, status: str, summary: str, turn: int,
    ) -> None:
        """Record a verification result for a session."""
        ...

    async def bulk_create_verifications(
        self, session_id: str, verifications: list[dict], turn: int
    ) -> list[str]:
        """Batch create Verification nodes (single UNWIND). Returns verification_ids."""
        ...

    async def get_last_verification(self, session_id: str) -> dict | None:
        """Get the most recent verification for a session."""
        ...

    # ── Cleanup / TTL ──────────────────────────────────────────────────

    async def expire_sessions(self) -> int:
        """Mark sessions past their TTL as expired. Returns count expired."""
        ...

    async def delete_expired_sessions(self) -> int:
        """Delete all nodes/edges for expired sessions. Returns count deleted."""
        ...

    async def delete_session_data(self, session_id: str) -> dict:
        """Delete all graph data for one session."""
        ...
