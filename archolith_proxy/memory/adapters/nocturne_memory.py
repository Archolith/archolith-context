"""Nocturne Memory adapter — promotes facts via the Nocturne Memory REST API.

Nocturne Memory (1.1k+ stars) is a long-term memory server for MCP agents with
a unique URI-graph routing system. It stores memories as nodes in a hierarchical
URI namespace (e.g. `core://agent/identity`) with version control, rollback,
disclosure triggers, glossary auto-hyperlinking, and a visual dashboard.

This adapter promotes facts as nodes under a configurable domain.
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
    """Promotes facts into Nocturne Memory via its REST API.

    Expected engine config:
    - base_url: e.g. "http://localhost:8233"
    - api_key_env: env var name for API_TOKEN
    - extra["domain"]: URI domain for promoted memories (default: "promoted")
    - extra["parent_uri"]: parent node URI (default: "promoted://archolith-proxy")
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    @property
    def _domain(self) -> str:
        return self.config.extra.get("domain", "promoted")

    @property
    def _parent_uri(self) -> str:
        return self.config.extra.get("parent_uri", f"{self._domain}://archolith-proxy")

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
            problems.append("base_url is required for nocturne_memory adapter")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,
            list_by_source=True,
            update_promoted=True,  # Nocturne supports update_memory
            delete_promoted=True,  # Nocturne supports delete_memory
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
        """Create a memory node via POST /api/memories."""
        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)
            resp = await client.post("/api/memories", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201):
                data = resp.json()
                remote_id = data.get("path") or data.get("uri") or data.get("id", "")
                logger.info(
                    "nocturne_promoted",
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
        """List memories under the parent URI via GET /api/memories."""
        try:
            client = self._get_client()
            resp = await client.get(
                "/api/memories",
                params={"parent": self._parent_uri, "search": session_id},
            )
            if resp.status_code == 200:
                return resp.json().get("memories", [])
            return []
        except Exception:
            return []

    async def update_promoted_memory(self, remote_id: str, promotion: PromotionRecord) -> PromotionResult:
        """Update a memory node via PATCH /api/memories/{id}."""
        start = time.monotonic()
        try:
            client = self._get_client()
            payload = {"content": promotion.content, "metadata": self._build_payload(promotion).get("metadata", {})}
            resp = await client.patch(f"/api/memories/{remote_id}", json=payload)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 204):
                return PromotionResult(
                    promotion_id=promotion.promotion_id,
                    engine_id=self.config.id,
                    outcome=PromotionOutcome.SUCCESS,
                    remote_id=remote_id,
                    elapsed_ms=elapsed_ms,
                )
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

    async def delete_promoted_memory(self, remote_id: str) -> PromotionResult:
        """Delete a memory path via DELETE /api/memories/{id}."""
        start = time.monotonic()
        try:
            client = self._get_client()
            resp = await client.delete(f"/api/memories/{remote_id}")
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
        """Build Nocturne Memory create payload."""
        # Generate a URI-safe slug for the child node
        slug = self._slugify(f"{promotion.fact_type}-{promotion.promotion_id}")
        child_uri = f"{self._parent_uri}/{slug}"

        return {
            "parent_path": self._parent_uri,
            "path": child_uri,
            "content": promotion.content,
            "priority": min(10, int(promotion.confidence * 10)),  # 0-10 scale
            "disclosure": f"When discussing {promotion.fact_type} from session {promotion.session_id}",
            "metadata": {
                "source": "archolith-proxy-promotion",
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

    @staticmethod
    def _slugify(text: str) -> str:
        import re
        slug = text.lower().strip()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        return slug[:60].strip("-")
