"""Cognee adapter — promotes facts via Cognee's remember API.

Cognee (17k+ stars) is a memory control plane for AI agents that combines
embeddings, graphs, and cognitive science approaches. It exposes a simple
Python SDK with `remember`, `recall`, `forget`, and `improve` operations.

This adapter promotes facts through Cognee's REST API (self-hosted or cloud).
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
    """Promotes facts into Cognee via its REST API.

    Expected engine config:
    - base_url: Cognee service URL (e.g. "http://localhost:8000" for local, or cloud URL)
    - api_key_env: env var name for COGNEE_API_KEY
    - extra["dataset"]: dataset name (default: "archolith-proxy")
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    @property
    def _dataset(self) -> str:
        return self.config.extra.get("dataset", "archolith-proxy")

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
                timeout=60.0,  # Cognee pipelines can take longer
            )
        return self._client

    async def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.config.base_url:
            problems.append("base_url is required for cognee adapter")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,
            list_by_source=False,
            update_promoted=False,
            delete_promoted=True,  # Cognee has `forget`
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
        """Add a fact via Cognee's remember/ingest endpoint."""
        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)

            # Cognee uses /api/v1/ingest or SDK-equivalent REST endpoint
            resp = await client.post("/api/v1/ingest", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201, 202):
                data = {}
                try:
                    data = resp.json()
                except Exception:
                    pass
                remote_id = data.get("id") or data.get("run_id", "")
                logger.info(
                    "cognee_promoted",
                    promotion_id=promotion.promotion_id,
                    dataset=self._dataset,
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

    async def delete_promoted_memory(self, remote_id: str) -> PromotionResult:
        """Delete via Cognee's forget endpoint."""
        start = time.monotonic()
        try:
            client = self._get_client()
            resp = await client.delete(f"/api/v1/datasets/{self._dataset}", json={"id": remote_id})
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
        """Build Cognee ingest payload."""
        return {
            "content": promotion.content,
            "dataset": self._dataset,
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
