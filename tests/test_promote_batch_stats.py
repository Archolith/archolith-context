"""Tests for promote_batch stats recording (Phase 8 fix)."""

from __future__ import annotations

import pytest

from archolith_proxy.memory.models import (
    PromotionOutcome,
    PromotionRecord,
)
from archolith_proxy.memory.promotion import PromotionService
from archolith_proxy.memory.registry import reset_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    """Ensure registry is fresh for each test."""
    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# Test promote_batch stats recording on all paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_batch_dry_run_records_stats():
    """promote_batch records stats for dry-run results (not just successes)."""
    svc = PromotionService(
        promotable_types={"decision"},
        min_confidence=0.0,
    )

    promotions = [
        PromotionRecord(
            session_id="sess-1",
            source_turn=1,
            fact_type="decision",
            content="choice 1",
            confidence=0.9,
            promotion_reason="test",
        ),
        PromotionRecord(
            session_id="sess-1",
            source_turn=1,
            fact_type="decision",
            content="choice 2",
            confidence=0.9,
            promotion_reason="test",
        ),
    ]

    # Call promote_batch with dry_run=True (no adapter available)
    results = await svc.promote_batch(promotions, dry_run=True)

    # Should return results (all skipped)
    assert len(results) == 2
    assert all(r.outcome == PromotionOutcome.SKIPPED for r in results)

    # Stats should be recorded (not empty)
    stats = svc.stats
    assert stats["attempted"] == 2
    assert stats["skipped"] == 2
    assert stats["succeeded"] == 0
    assert stats["failed"] == 0

    # Audit trail should include these results
    assert len(svc.audit_trail) == 2


@pytest.mark.asyncio
async def test_promote_batch_no_adapter_records_stats():
    """promote_batch records stats when no adapter is configured."""
    svc = PromotionService(
        promotable_types={"decision"},
        min_confidence=0.0,
    )
    # Don't register any adapter

    promotions = [
        PromotionRecord(
            session_id="sess-1",
            source_turn=1,
            fact_type="decision",
            content="choice",
            confidence=0.9,
            promotion_reason="test",
        ),
    ]

    results = await svc.promote_batch(promotions)

    # Should record stats for no-adapter case
    stats = svc.stats
    assert stats["attempted"] == 1
    assert stats["skipped"] == 1
    assert "No memory engine configured" in results[0].error_message


@pytest.mark.asyncio
async def test_promote_batch_mixed_outcomes_recorded():
    """promote_batch correctly counts SUCCESS, FAILED, SKIPPED in audit trail."""
    svc = PromotionService()

    # Create one promotion
    promotions = [
        PromotionRecord(
            session_id="sess-1",
            source_turn=1,
            fact_type="observation",  # Promotable type
            content="test fact",
            confidence=0.95,  # High confidence
            promotion_reason="test",
        ),
    ]

    # With no adapter, it will be skipped
    results = await svc.promote_batch(promotions)

    # Verify stats were recorded
    assert svc.stats["attempted"] >= 1
    assert len(svc.audit_trail) == len(results)


@pytest.mark.asyncio
async def test_promote_batch_empty_input():
    """promote_batch handles empty list gracefully."""
    svc = PromotionService()

    results = await svc.promote_batch([])

    # Should return empty, no stats recorded
    assert results == []
    assert svc.stats["attempted"] == 0
    assert len(svc.audit_trail) == 0
