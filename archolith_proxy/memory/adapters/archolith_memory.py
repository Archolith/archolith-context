"""First-party adapter for archolith-memory — the long-term durable memory system.

This adapter promotes facts via the archolith-memory HTTP API, which is the
same backend used by the memory MCP tools (add_memory, recall_memories, etc.).
"""

from __future__ import annotations

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
    """Promotes facts into cth.mcp.memory via its HTTP REST API.

    Expected engine config:
    - base_url: e.g. "http://localhost:8200"
    - api_key_env: env var name for the API key
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    @property
    def _base_url(self) -> str:
        return self.config.base_url.rstrip("/")

    @property
    def _api_key(self) -> str:
        return self.config.resolved_api_key

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def validate_config(self) -> list[str]:
        """Validate that the engine config has what we need."""
        problems: list[str] = []
        if not self.config.base_url:
            problems.append("base_url is required for cth_mcp_memory adapter")
        if not self.config.base_url.startswith(("http://", "https://")):
            problems.append(f"base_url must be http/https, got: {self.config.base_url}")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,  # cth.mcp.memory handles dedupe internally
            list_by_source=True,
            update_promoted=False,
            delete_promoted=False,
            healthcheck=True,
        )

    async def healthcheck(self) -> bool:
        """Check if the memory API is reachable."""
        try:
            client = self._get_client()
            resp = await client.get("/health")
            return resp.status_code == 200
        except Exception:
            logger.debug("archolith_memory_healthcheck_failed", base_url=self._base_url)
            return False

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        """Promote a single fact via POST /api/v1/memories."""
        import time

        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_add_memory_payload(promotion)
            resp = await client.post("/api/v1/memories", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201):
                data = resp.json()
                remote_id = data.get("uuid") or data.get("id", "")
                logger.info(
                    "archolith_memory_promoted",
                    promotion_id=promotion.promotion_id,
                    remote_id=remote_id,
                    session_id=promotion.session_id,
                )
                return PromotionResult(
                    promotion_id=promotion.promotion_id,
                    engine_id=self.config.id,
                    outcome=PromotionOutcome.SUCCESS,
                    remote_id=remote_id,
                    elapsed_ms=elapsed_ms,
                )
            else:
                error_msg = f"HTTP {resp.status_code}: {resp.text[:500]}"
                logger.warning(
                    "archolith_memory_promote_failed",
                    promotion_id=promotion.promotion_id,
                    status=resp.status_code,
                )
                return PromotionResult(
                    promotion_id=promotion.promotion_id,
                    engine_id=self.config.id,
                    outcome=PromotionOutcome.FAILED,
                    error_message=error_msg,
                    elapsed_ms=elapsed_ms,
                )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("archolith_memory_promote_error", promotion_id=promotion.promotion_id)
            return PromotionResult(
                promotion_id=promotion.promotion_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

    async def promote_batch(self, promotions: list[PromotionRecord]) -> list[PromotionResult]:
        """Batch promote — sequential calls (memory API doesn't have a batch endpoint)."""
        results: list[PromotionResult] = []
        for p in promotions:
            result = await self.promote_fact(p)
            results.append(result)
        return results

    async def list_memories_by_source(self, session_id: str) -> list[dict]:
        """List memories promoted from a given session via GET /api/v1/memories."""
        try:
            client = self._get_client()
            resp = await client.get(
                "/api/v1/memories",
                params={"source_session_id": session_id, "limit": 100},
            )
            if resp.status_code == 200:
                return resp.json().get("memories", [])
            return []
        except Exception:
            logger.debug("archolith_memory_list_failed", session_id=session_id)
            return []

    # --- Internal ---

    def _build_add_memory_payload(self, promotion: PromotionRecord) -> dict:
        """Translate a canonical PromotionRecord into cth.mcp.memory's add_memory payload."""
        return {
            "text": promotion.content,
            "source": "archolith-proxy-promotion",
            "type": "SEMANTIC",
            "session_id": promotion.session_id,
            "metadata": {
                "promotion_id": promotion.promotion_id,
                "fact_type": promotion.fact_type,
                "confidence": promotion.confidence,
                "source_turn": promotion.source_turn,
                "session_goal": promotion.session_goal,
                "promotion_reason": promotion.promotion_reason,
                "touched_files": promotion.touched_files,
                "tags": promotion.tags,
                "dedupe_key": promotion.dedupe_key,
            },
        }
