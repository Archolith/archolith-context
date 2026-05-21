"""OpenMemory adapter — promotes facts via the OpenMemory REST API.

OpenMemory (4k+ stars) is a cognitive memory engine for LLMs — multi-sector
(episodic, semantic, procedural, emotional, reflective), temporal knowledge
graph, decay & reinforcement, waypoint graph, and explainable traces.

Self-hosted on SQLite/Postgres with SDKs in Python + Node and MCP server.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import structlog

from archolith_proxy.memory.adapters.base import MemoryAdapterBase
from archolith_proxy.memory.models import (
    EngineCapabilities,
    PromotionOutcome,
    PromotionResult,
)

if TYPE_CHECKING:
    from archolith_proxy.memory.models import MemoryEngineConfig, PromotionRecord

logger = structlog.get_logger()


class Adapter(MemoryAdapterBase):
    """Promotes facts into OpenMemory via its REST API.

    Expected engine config:
    - base_url: e.g. "http://localhost:8080"
    - api_key_env: env var name for API key (if auth enabled)
    - extra["user_id"]: user ID for OpenMemory's per-user scoping (default: "context-engine")
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    @property
    def _user_id(self) -> str:
        return self.config.extra.get("user_id", "context-engine")

    @property
    def _api_key(self) -> str:
        return self.config.resolved_api_key

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.config.base_url:
            problems.append("base_url is required for openmemory adapter")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,
            list_by_source=True,
            update_promoted=False,
            delete_promoted=True,
            healthcheck=True,
        )

    async def healthcheck(self) -> bool:
        try:
            client = self._get_client()
            resp = await client.get("/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        """Add a memory via POST /api/memory/add."""
        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)
            resp = await client.post("/api/memory/add", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201):
                data = resp.json()
                remote_id = data.get("id") or data.get("memory_id", "")
                logger.info(
                    "openmemory_promoted",
                    promotion_id=promotion.promotion_id,
                    remote_id=remote_id,
                )
                return PromotionResult(
                    promotion_id=promotion.promotion_id,
                    engine_id=self.config.id,
                    outcome=PromotionOutcome.SUCCESS,
                    remote_id=remote_id or None,
                    elapsed_ms=elapsed_ms,
                )
            else:
                return PromotionResult(
                    promotion_id=promotion.promotion_id,
                    engine_id=self.config.id,
                    outcome=PromotionOutcome.FAILED,
                    error_message=f"HTTP {resp.status_code}: {resp.text[:500]}",
                    elapsed_ms=elapsed_ms,
                )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return PromotionResult(
                promotion_id=promotion.promotion_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

    async def list_memories_by_source(self, session_id: str) -> list[dict]:
        """List memories for a session via GET /api/memory/search."""
        try:
            client = self._get_client()
            resp = await client.get(
                "/api/memory/search",
                params={"query": session_id, "user_id": self._user_id, "limit": 100},
            )
            if resp.status_code == 200:
                return resp.json().get("results", [])
            return []
        except Exception:
            return []

    async def delete_promoted_memory(self, remote_id: str) -> PromotionResult:
        """Delete a memory via DELETE /api/memory/{id}."""
        start = time.monotonic()
        try:
            client = self._get_client()
            resp = await client.delete(f"/api/memory/{remote_id}")
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 204):
                return PromotionResult(
                    remote_id=remote_id,
                    engine_id=self.config.id,
                    outcome=PromotionOutcome.SUCCESS,
                    elapsed_ms=elapsed_ms,
                )
            return PromotionResult(
                remote_id=remote_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=f"HTTP {resp.status_code}: {resp.text[:500]}",
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return PromotionResult(
                remote_id=remote_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

    # --- Internal ---

    def _build_payload(self, promotion: PromotionRecord) -> dict:
        """Build OpenMemory add payload."""
        return {
            "content": promotion.content,
            "user_id": self._user_id,
            "metadata": {
                "source": "context-engine-promotion",
                "promotion_id": promotion.promotion_id,
                "fact_type": promotion.fact_type,
                "confidence": promotion.confidence,
                "session_id": promotion.session_id,
                "source_turn": promotion.source_turn,
                "session_goal": promotion.session_goal,
                "touched_files": promotion.touched_files,
                "tags": promotion.tags,
                "dedupe_key": promotion.dedupe_key,
            },
        }
