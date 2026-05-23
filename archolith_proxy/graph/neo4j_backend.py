"""Neo4j graph backend — wraps existing session/fact/edge/decision/cleanup modules.

Implements the GraphBackend protocol by delegating to the existing Neo4j-coupled
module functions in archolith_proxy/graph/. This adapter preserves the current label-guard,
index creation, and driver lifecycle while exposing the protocol interface.
"""

from __future__ import annotations

import structlog

from archolith_proxy.graph import cleanup as _cleanup
from archolith_proxy.graph import decisions as _decisions
from archolith_proxy.graph import edges as _edges
from archolith_proxy.graph import facts as _facts
from archolith_proxy.graph import session as _session
from archolith_proxy.graph.driver import close_driver, ensure_indexes, get_driver, init_driver
from archolith_proxy.graph.protocol import GraphBackend

logger = structlog.get_logger()


class Neo4jBackend:
    """Neo4j implementation of the GraphBackend protocol.

    Delegates all operations to the existing archolith_proxy/graph/ module functions.
    The label-guard isolation (CONTEXT_SESSION_LABEL) is applied transparently
    by the repository layer.
    """

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize the Neo4j driver and create indexes."""
        await init_driver()
        await ensure_indexes()

    async def close(self) -> None:
        """Close the Neo4j driver."""
        await close_driver()

    async def ensure_schema(self) -> None:
        """Create required indexes and constraints (safe idempotent call)."""
        await ensure_indexes()

    async def verify_connectivity(self) -> bool:
        """Verify Neo4j connectivity."""
        try:
            driver = await get_driver()
            await driver.verify_connectivity()
            return True
        except Exception:
            return False

    def is_ready(self) -> bool:
        """Check if the driver is initialized and connected."""
        try:
            # get_driver() raises RuntimeError if not initialized
            import asyncio

            # Can't call async verify from sync context, just check driver existence
            from archolith_proxy.graph.driver import _driver
            return _driver is not None
        except Exception:
            return False

    # ── Session CRUD ───────────────────────────────────────────────────

    async def create_session(
        self, session_id: str, fingerprint: str | None = None
    ) -> dict:
        return await _session.create_session(session_id, fingerprint)

    async def find_session_by_id(self, session_id: str) -> dict | None:
        return await _session.find_by_session_id(session_id)

    async def find_session_by_fingerprint(self, fingerprint: str) -> dict | None:
        return await _session.find_by_fingerprint(fingerprint)

    async def find_or_create_by_fingerprint(
        self, fingerprint: str
    ) -> tuple[dict, bool]:
        return await _session.find_or_create_by_fingerprint(fingerprint)

    async def touch_session(self, session_id: str) -> None:
        await _session.touch_session(session_id)

    async def get_turn_number(self, session_id: str) -> int:
        return await _session.get_turn_number(session_id)

    async def update_goal(self, session_id: str, goal: str) -> None:
        await _session.update_goal(session_id, goal)

    async def list_active_sessions(self) -> list[dict]:
        return await _session.list_active_sessions()

    async def get_session_stats(self, session_id: str) -> dict:
        return await _session.get_session_stats(session_id)

    # ── Fact CRUD ──────────────────────────────────────────────────────

    async def store_fact(
        self,
        session_id: str,
        content: str,
        fact_type: str,
        source_turn: int,
        confidence: float = 0.5,
        embedding: list[float] | None = None,
    ) -> str:
        return await _facts.store_fact(
            session_id=session_id,
            content=content,
            fact_type=fact_type,
            source_turn=source_turn,
            confidence=confidence,
            embedding=embedding,
        )

    async def store_facts_batch(
        self,
        session_id: str,
        facts: list[dict],
        source_turn: int,
    ) -> list[str]:
        return await _facts.store_facts_batch(session_id, facts, source_turn)

    async def invalidate_facts(self, fact_ids: list[str]) -> int:
        return await _facts.invalidate_facts(fact_ids)

    async def find_matching_fact_ids(
        self, session_id: str, descriptions: list[str]
    ) -> list[str]:
        return await _facts.find_matching_fact_ids(session_id, descriptions)

    async def get_active_facts(
        self, session_id: str, limit: int = 50
    ) -> list[dict]:
        return await _facts.get_active_facts(session_id, limit)

    async def get_active_fact_count(self, session_id: str) -> int:
        return await _facts.get_active_fact_count(session_id)

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
        return await _facts.get_facts_filtered(
            session_id=session_id,
            fact_type=fact_type,
            min_confidence=min_confidence,
            from_turn=from_turn,
            to_turn=to_turn,
            include_invalidated=include_invalidated,
            limit=limit,
        )

    async def get_invalidated_facts(self, session_id: str) -> list[dict]:
        return await _facts.get_invalidated_facts(session_id)

    async def get_supersession_chain(self, session_id: str) -> list[dict]:
        return await _facts.get_supersession_chain(session_id)

    # ── Edge Operations ────────────────────────────────────────────────

    async def create_belongs_to(self, session_id: str, fact_id: str) -> None:
        await _edges.create_belongs_to(session_id, fact_id)

    async def create_touches(
        self, session_id: str, file_path: str, status: str, turn: int
    ) -> None:
        await _edges.create_touches(session_id, file_path, status, turn)

    async def create_supersedes(
        self, old_fact_id: str, new_fact_id: str
    ) -> None:
        await _edges.create_supersedes(old_fact_id, new_fact_id)

    async def get_touched_files(self, session_id: str) -> list[dict]:
        return await _edges.get_touched_files(session_id)

    # ── Decision Operations ────────────────────────────────────────────

    async def store_decision(
        self,
        session_id: str,
        summary: str,
        rationale: str | None,
        turn: int,
    ) -> str:
        return await _decisions.store_decision(
            session_id=session_id,
            summary=summary,
            rationale=rationale,
            turn=turn,
        )

    async def get_decisions(
        self, session_id: str, include_superseded: bool = False
    ) -> list[dict]:
        return await _decisions.get_decisions(session_id, include_superseded)

    # ── File Content Cache (LadybugDB-only in MVP — stubs for Neo4j) ───

    async def upsert_file_content(
        self, session_id: str, path: str, content: str, sha256: str, turn: int,
    ) -> None:
        return None

    async def get_file_content(self, session_id: str, path: str) -> dict | None:
        return None

    async def get_file_lines(
        self, session_id: str, path: str, start: int, end: int,
    ) -> str | None:
        return None

    async def list_cached_files(self, session_id: str) -> list[dict]:
        return []

    # ── Cleanup / TTL ──────────────────────────────────────────────────

    async def expire_sessions(self) -> int:
        return await _cleanup.expire_sessions()

    async def delete_expired_sessions(self) -> int:
        return await _cleanup.delete_expired_sessions()


# Verify the adapter implements the protocol at import time
def _verify_protocol() -> None:
    if not isinstance(Neo4jBackend, GraphBackend):
        logger.warning(
            "neo4j_backend_protocol_mismatch",
            note="Neo4jBackend does not satisfy GraphBackend protocol",
        )


_verify_protocol()
