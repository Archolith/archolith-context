"""Mem0 memory adapter — basic fact/observation write adapter.

Mem0 is a popular memory layer for AI agents. This adapter sends facts
via the Mem0 REST API. Write-focused: no read path, no sync.
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
    """Promotes facts into Mem0 via its REST API.

    Expected engine config:
    - base_url: e.g. "https://api.mem0.ai/v1"
    - api_key_env: env var name for the Mem0 API key
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
                headers["Authorization"] = f"Token {self._api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.config.base_url:
            problems.append("base_url is required for mem0 adapter")
        if not self.config.api_key_env:
            problems.append("api_key_env is required for mem0 adapter")
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
        """Add a memory via POST /memories/."""
        import time

        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)
            resp = await client.post("/memories/", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201):
                data = resp.json()
                remote_id = data.get("id", "")
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
        """Translate a canonical PromotionRecord into Mem0's add memory payload."""
        return {
            "messages": [{"role": "user", "content": promotion.content}],
            "metadata": {
                "source": "context-engine",
                "fact_type": promotion.fact_type,
                "confidence": promotion.confidence,
                "session_id": promotion.session_id,
                "promotion_id": promotion.promotion_id,
                "tags": promotion.tags,
            },
        }
