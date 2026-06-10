"""Promotion service — decides what gets promoted, routes to adapters, tracks audit.

The promotion service sits between the extraction pipeline (which identifies
facts) and the adapter layer (which writes them to durable memory). It applies
promotion policy, generates canonical promotion records, and persists audit
status for observability.
"""

from __future__ import annotations

__all__ = ["PromotionService"]

import json
import os
import time

import structlog

from archolith_proxy.memory.adapters.base import MemoryAdapterBase
from archolith_proxy.memory.models import (
    PromotionOutcome,
    PromotionRecord,
    PromotionResult,
)
from archolith_proxy.memory.registry import MemoryEngineRegistry, get_registry

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Promotion policy — start conservative
# ---------------------------------------------------------------------------

# Fact types eligible for automatic promotion
_PROMOTABLE_FACT_TYPES: set[str] = {
    "decision",
    "file_state",
    "observation",
    "state",
}

# Minimum confidence for automatic promotion
_MIN_CONFIDENCE = 0.9

# NOTE: Multi-turn survival gate removed (2026-06-01).
# The gate received session turn_number instead of fact age, so it
# rubber-stamped everything after turn 2.  The extraction pipeline
# doesn't re-confirm facts across turns, so there's no real survival
# signal to gate on.  Reintroduce when a deterministic fact-lifecycle
# layer exists.


class PromotionService:
    """Orchestrates fact promotion from session-local to durable memory.

    Responsibilities:
    1. Decide whether a fact qualifies for promotion (policy)
    2. Map extracted facts into canonical promotion records
    3. Choose target engine(s)
    4. Invoke adapter
    5. Track audit status
    """

    def __init__(
        self,
        registry: MemoryEngineRegistry | None = None,
        promotable_types: set[str] | None = None,
        min_confidence: float = _MIN_CONFIDENCE,
        audit_dir: str | None = None,
    ) -> None:
        self.registry = registry or get_registry()
        self.promotable_types = promotable_types or _PROMOTABLE_FACT_TYPES
        self.min_confidence = min_confidence

        # In-memory audit trail (process-level, reset on restart).
        self._audit: list[PromotionResult] = []
        self._stats = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }

        # D5: optional durable audit trail. When a directory is configured the
        # in-memory trail is mirrored to a JSONL file so it survives restarts.
        if audit_dir is None:
            from archolith_proxy.config import get_settings
            audit_dir = get_settings().promotion_audit_dir
        self._audit_path: str | None = None
        if audit_dir:
            try:
                os.makedirs(audit_dir, exist_ok=True)
                self._audit_path = os.path.join(audit_dir, "promotion_audit.jsonl")
            except OSError as e:
                logger.warning("promotion_audit_dir_unusable", dir=audit_dir, error=str(e))

    # --- Policy ---

    def should_promote(
        self,
        fact_type: str,
        confidence: float,
        tags: list[str] | None = None,
        *,
        explicit: bool = False,
    ) -> bool:
        """Decide whether a fact qualifies for promotion.

        Policy rules:
        - Explicit operator promotion always goes through
        - fact_type must be in the promotable allowlist
        - confidence must meet threshold
        """
        if explicit:
            return True

        if fact_type not in self.promotable_types:
            return False

        if confidence < self.min_confidence:
            return False

        return True

    # --- Promotion ---

    async def promote_fact(
        self,
        promotion: PromotionRecord,
        engine_id: str | None = None,
        *,
        dry_run: bool = False,
    ) -> PromotionResult:
        """Promote a single fact to a memory engine.

        Args:
            promotion: Canonical promotion record
            engine_id: Explicit engine target, or None for default
            dry_run: If True, generate the record but don't actually promote

        Returns:
            PromotionResult with outcome and metadata
        """
        start = time.monotonic()

        # Resolve engine
        adapter = self._resolve_adapter(engine_id)
        if adapter is None:
            result = PromotionResult(
                promotion_id=promotion.promotion_id,
                engine_id=engine_id or "none",
                outcome=PromotionOutcome.SKIPPED,
                error_message="No memory engine configured or available",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )
            self._record(result)
            return result

        # Auto-dedupe key
        record = promotion.with_auto_dedupe()

        # Dry run — skip actual write
        if dry_run:
            result = PromotionResult(
                promotion_id=record.promotion_id,
                engine_id=adapter.config.id,
                outcome=PromotionOutcome.SKIPPED,
                error_message="Dry run — promotion not attempted",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )
            self._record(result)
            return result

        # Dedupe check (if adapter supports it)
        caps = await adapter.capabilities()
        if caps.dedupe_lookup and record.dedupe_key:
            existing = await adapter.dedupe_lookup(record)
            if existing is not None:
                result = PromotionResult(
                    promotion_id=record.promotion_id,
                    engine_id=adapter.config.id,
                    outcome=PromotionOutcome.SKIPPED,
                    remote_id=existing,
                    error_message=f"Dedupe hit: fact already promoted as {existing}",
                    elapsed_ms=(time.monotonic() - start) * 1000,
                )
                self._record(result)
                return result

        # Promote
        try:
            result = await adapter.promote_fact(record)
        except Exception as exc:
            logger.exception("promotion_failed", promotion_id=record.promotion_id, engine_id=adapter.config.id)
            result = PromotionResult(
                promotion_id=record.promotion_id,
                engine_id=adapter.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=str(exc),
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        result.elapsed_ms = (time.monotonic() - start) * 1000
        self._record(result)
        return result

    async def promote_batch(
        self,
        promotions: list[PromotionRecord],
        engine_id: str | None = None,
        *,
        dry_run: bool = False,
    ) -> list[PromotionResult]:
        """Promote a batch of facts. Routes to adapter's promote_batch if supported.

        Records stats and audit for all outcomes: success, skipped, failed.
        """
        if not promotions:
            return []

        adapter = self._resolve_adapter(engine_id)
        if adapter is None:
            # All skipped
            results = [
                PromotionResult(
                    promotion_id=p.promotion_id,
                    engine_id=engine_id or "none",
                    outcome=PromotionOutcome.SKIPPED,
                    error_message="No memory engine configured or available",
                )
                for p in promotions
            ]
            for r in results:
                self._record(r)
            return results

        if dry_run:
            results = [
                PromotionResult(
                    promotion_id=p.promotion_id,
                    engine_id=adapter.config.id,
                    outcome=PromotionOutcome.SKIPPED,
                    error_message="Dry run",
                )
                for p in promotions
            ]
            for r in results:
                self._record(r)
            return results

        # Use adapter batch method
        records = [p.with_auto_dedupe() for p in promotions]
        try:
            results = await adapter.promote_batch(records)
            for r in results:
                self._record(r)
            return results
        except Exception as exc:
            logger.exception("batch_promotion_failed", engine_id=adapter.config.id)
            results = [
                PromotionResult(
                    promotion_id=p.promotion_id,
                    engine_id=adapter.config.id,
                    outcome=PromotionOutcome.FAILED,
                    error_message=str(exc),
                )
                for p in promotions
            ]
            for r in results:
                self._record(r)
            return results

    # --- Observability ---

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def audit_trail(self) -> list[PromotionResult]:
        return list(self._audit)

    # --- Internal ---

    def _resolve_adapter(self, engine_id: str | None = None) -> "MemoryAdapterBase | None":
        """Resolve adapter by explicit id or default."""
        if engine_id is not None:
            return self.registry.get_adapter(engine_id)
        return self.registry.get_default_adapter()

    def _record(self, result: PromotionResult) -> None:
        """Track result in audit trail and stats; persist to JSONL if configured."""
        self._audit.append(result)
        self._stats["attempted"] += 1
        if result.outcome == PromotionOutcome.SUCCESS:
            self._stats["succeeded"] += 1
        elif result.outcome == PromotionOutcome.FAILED:
            self._stats["failed"] += 1
        elif result.outcome == PromotionOutcome.SKIPPED:
            self._stats["skipped"] += 1

        # D5: append to the durable audit trail when configured. Best-effort —
        # a persistence failure must never break promotion.
        if self._audit_path:
            try:
                line = json.dumps(result.model_dump(), default=str)
                with open(self._audit_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception as e:
                logger.warning("promotion_audit_persist_failed", error=str(e))
