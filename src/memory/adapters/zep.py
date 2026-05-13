"""Zep memory adapter — basic memory/fact write adapter.

Zep is a long-term memory service for AI agents. This adapter sends facts
via the Zep REST API. Write-focused only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import structlog

from src.memory.adapters.base import MemoryAdapterBase
from src.memory.models import (
    EngineCapabilities,
    PromotionOutcome,
    PromotionResult,
)

if TYPE_CHECKING:
    from src.memory.models import MemoryEngineConfig, PromotionRecord

logger = structlog.get_logger()


class Adapter(MemoryAdapterBase):
    """Promotes facts into Zep via its REST API.

    Expected engine config:
    - base_url: e.g. "https://api.getzep.com/api/v2"
    - api_key_env: env var name for the Zep API key
    - extra["user_id"]: optional user ID for Zep's user-scoped memories
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
            problems.append("base_url is required for zep adapter")
        if not self.config.api_key_env:
            problems.append("api_key_env is required for zep adapter")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,
            list_by_source=False,
            update_promoted=False,
            delete_promoted=False,
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
        """Add a fact via POST /facts."""
        import time

        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)
            resp = await client.post("/facts", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201):
                data = resp.json()
                remote_id = data.get("uuid") or data.get("id", "")
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
        """Translate a canonical PromotionRecord into Zep's fact payload."""
        user_id = self.config.extra.get("user_id", "context-engine")
        return {
            "user_id": user_id,
            "fact": promotion.content,
            "metadata": {
                "source": "context-engine",
                "fact_type": promotion.fact_type,
                "confidence": promotion.confidence,
                "session_id": promotion.session_id,
                "promotion_id": promotion.promotion_id,
                "tags": promotion.tags,
            },
        }
