"""Abstract adapter contract — all memory engine adapters must implement this.

The adapter interface is deliberately narrow: validate config, report capabilities,
check health, and promote facts. Read-side methods are optional and should only
be added when a concrete integration need exists, not speculatively.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.models import EngineCapabilities, MemoryEngineConfig, PromotionRecord, PromotionResult


class MemoryAdapterBase(abc.ABC):
    """Base class for all memory engine adapters.

    Subclasses must implement:
    - validate_config()
    - capabilities()
    - healthcheck()
    - promote_fact()

    Optional overrides:
    - promote_batch()  (default: sequential loop over promote_fact)
    - dedupe_lookup()
    - list_memories_by_source()
    - update_promoted_memory()
    - delete_promoted_memory()
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        self.config = config

    # --- Required ---

    @abc.abstractmethod
    async def validate_config(self) -> list[str]:
        """Validate the engine config. Return a list of problems (empty = valid)."""

    @abc.abstractmethod
    async def capabilities(self) -> EngineCapabilities:
        """Report what this adapter supports."""

    @abc.abstractmethod
    async def healthcheck(self) -> bool:
        """Return True if the backend is reachable and healthy."""

    @abc.abstractmethod
    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        """Promote a single fact into the target memory system."""

    # --- Optional (sensible defaults) ---

    async def promote_batch(self, promotions: list[PromotionRecord]) -> list[PromotionResult]:
        """Promote a batch of facts. Default: sequential promote_fact calls."""
        return [await self.promote_fact(p) for p in promotions]

    async def dedupe_lookup(self, promotion: PromotionRecord) -> str | None:
        """Check if a fact already exists in the target. Return remote ID or None.

        Only called if capabilities().dedupe_lookup is True.
        """
        return None

    async def list_memories_by_source(self, session_id: str) -> list[dict]:
        """List memories promoted from a given session. Return simplified dicts.

        Only called if capabilities().list_by_source is True.
        """
        return []

    async def update_promoted_memory(self, remote_id: str, promotion: PromotionRecord) -> PromotionResult:
        """Update a previously promoted memory. Only if capabilities().update_promoted."""

        from src.memory.models import PromotionResult, PromotionOutcome

        return PromotionResult(
            promotion_id=promotion.promotion_id,
            engine_id=self.config.id,
            outcome=PromotionOutcome.SKIPPED,
            error_message="update_promoted_memory not supported",
        )

    async def delete_promoted_memory(self, remote_id: str) -> PromotionResult:
        """Delete a previously promoted memory. Only if capabilities().delete_promoted."""

        from src.memory.models import PromotionOutcome, PromotionResult

        return PromotionResult(
            engine_id=self.config.id,
            outcome=PromotionOutcome.SKIPPED,
            error_message="delete_promoted_memory not supported",
        )
