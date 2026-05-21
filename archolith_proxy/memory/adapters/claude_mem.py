"""claude-mem adapter — promotes facts via the claude-mem worker service HTTP API.

claude-mem is a persistent memory compression system (75k+ stars) that captures
agent activity, compresses it with AI, and injects relevant context into future
sessions. It runs a local worker service on port 37777 with SQLite + ChromaDB.

This adapter promotes facts as observations into the claude-mem worker.
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
    """Promotes facts into claude-mem via its worker HTTP API.

    Expected engine config:
    - base_url: e.g. "http://localhost:37777" (default worker port)
    - api_key_env: env var name for API key (if auth enabled)
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

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
            problems.append("base_url is required for claude-mem adapter")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,
            list_by_source=True,
            update_promoted=False,
            delete_promoted=False,
            healthcheck=True,
        )

    async def healthcheck(self) -> bool:
        try:
            client = self._get_client()
            resp = await client.get("/api/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        """Add an observation via the claude-mem worker API."""
        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)
            resp = await client.post("/api/observation", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201):
                data = resp.json()
                remote_id = str(data.get("id", ""))
                logger.info(
                    "claude_mem_promoted",
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

    # --- Internal ---

    def _build_payload(self, promotion: PromotionRecord) -> dict:
        """Build claude-mem observation payload."""
        return {
            "content": promotion.content,
            "type": promotion.fact_type,
            "session_id": promotion.session_id,
            "metadata": {
                "source": "archolith-proxy-promotion",
                "promotion_id": promotion.promotion_id,
                "confidence": promotion.confidence,
                "source_turn": promotion.source_turn,
                "session_goal": promotion.session_goal,
                "touched_files": promotion.touched_files,
                "tags": promotion.tags,
                "dedupe_key": promotion.dedupe_key,
            },
        }
