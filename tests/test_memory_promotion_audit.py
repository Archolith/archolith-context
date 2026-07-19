"""Tests for D5 — durable promotion audit trail (optional JSONL persistence)."""

from __future__ import annotations

import json


from archolith_proxy.memory.models import PromotionOutcome, PromotionResult
from archolith_proxy.memory.promotion import PromotionService


def _result(pid: str = "p1", outcome=PromotionOutcome.SUCCESS) -> PromotionResult:
    return PromotionResult(promotion_id=pid, engine_id="e1", outcome=outcome)


class TestPromotionAuditPersistence:
    def test_records_persisted_to_jsonl(self, tmp_path):
        svc = PromotionService(audit_dir=str(tmp_path))
        svc._record(_result("p1"))
        svc._record(_result("p2", PromotionOutcome.FAILED))

        audit_file = tmp_path / "promotion_audit.jsonl"
        assert audit_file.exists()
        lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        rec0 = json.loads(lines[0])
        assert rec0["promotion_id"] == "p1"
        assert rec0["outcome"] in ("success", PromotionOutcome.SUCCESS.value)

    def test_survives_new_instance(self, tmp_path):
        PromotionService(audit_dir=str(tmp_path))._record(_result("p1"))
        # A fresh process/instance can read the persisted trail from disk.
        audit_file = tmp_path / "promotion_audit.jsonl"
        lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["promotion_id"] == "p1"

    def test_no_file_when_dir_unset(self, tmp_path):
        svc = PromotionService(audit_dir="")
        svc._record(_result("p1"))
        assert svc._audit_path is None
        # In-memory trail still works.
        assert len(svc.audit_trail) == 1
        assert not list(tmp_path.iterdir())

    def test_stats_unaffected_by_persistence(self, tmp_path):
        svc = PromotionService(audit_dir=str(tmp_path))
        svc._record(_result("p1", PromotionOutcome.SUCCESS))
        svc._record(_result("p2", PromotionOutcome.SKIPPED))
        assert svc.stats["attempted"] == 2
        assert svc.stats["succeeded"] == 1
        assert svc.stats["skipped"] == 1
