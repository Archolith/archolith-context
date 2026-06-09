"""Generic HTTP memory adapter — config-driven POST target for systems without bespoke integration.

This adapter sends promotion payloads to any HTTP endpoint. It's the fallback
for memory systems that don't merit a dedicated adapter yet.
"""

from __future__ import annotations

__all__ = ["Adapter"]

from typing import TYPE_CHECKING
from urllib.parse import urlparse

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
    """Promotes facts to any HTTP endpoint via POST.

    Expected engine config:
    - base_url: the target URL for POST requests
    - api_key_env: env var name for auth (sent as Bearer token)
    - extra["payload_template"]: optional JSON template with {content}, {fact_type} placeholders
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
                base_url=self.config.base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.config.base_url:
            problems.append("base_url is required for generic_http adapter")
        else:
            parsed = urlparse(self.config.base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                problems.append(
                    "base_url must be an http(s) URL with a host "
                    f"(got: {self.config.base_url!r})"
                )
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

    async def close(self) -> None:
        """Close the httpx client if open."""
        client = getattr(self, "_client", None)
        if client is not None and not client.is_closed:
            await client.aclose()

    async def healthcheck(self) -> bool:
        """Best-effort health check — GET on base_url or GET /health.

        Returns True only for 2xx status. 401/403/404/429 are considered unhealthy.
        """
        try:
            client = self._get_client()
            # Try /health first, fall back to base URL
            for path in ("/health", "/"):
                try:
                    resp = await client.get(path)
                    if 200 <= resp.status_code < 300:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        """POST the canonical promotion payload to the configured endpoint."""
        import time

        start = time.monotonic()
        try:
            client = self._get_client()
            payload = self._build_payload(promotion)
            resp = await client.post("", json=payload)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in (200, 201, 202):
                data = {}
                try:
                    data = resp.json()
                except Exception:
                    pass
                remote_id = data.get("id") or data.get("uuid", "")
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
        """Build the JSON payload. Uses template from config.extra if provided."""
        template = self.config.extra.get("payload_template")
        if template and isinstance(template, dict):
            # Substitute placeholders
            payload = {}
            for key, value in template.items():
                if isinstance(value, str):
                    payload[key] = value.format(
                        content=promotion.content,
                        fact_type=promotion.fact_type,
                        confidence=promotion.confidence,
                        session_id=promotion.session_id,
                    )
                else:
                    payload[key] = value
            return payload

        # Default: send the full canonical record
        return promotion.model_dump(mode="json")

