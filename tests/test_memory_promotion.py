"""Tests for memory engine registry, canonical models, adapter base, and promotion service."""

from __future__ import annotations

import pytest

from archolith_proxy.memory.models import (
    EngineCapabilities,
    MemoryEngineConfig,
    PromotionOutcome,
    PromotionRecord,
    PromotionResult,
)
from archolith_proxy.memory.registry import MemoryEngineRegistry, get_registry, reset_registry
from archolith_proxy.memory.promotion import PromotionService
from archolith_proxy.memory.adapters.base import MemoryAdapterBase


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_registry():
    """Ensure registry is fresh for each test."""
    reset_registry()
    yield
    reset_registry()


def _make_config(**overrides) -> MemoryEngineConfig:
    defaults = {
        "id": "test-engine",
        "type": "generic_http",
        "enabled": True,
        "priority": 10,
        "base_url": "http://localhost:9999",
    }
    defaults.update(overrides)
    return MemoryEngineConfig(**defaults)


def _make_promotion(**overrides) -> PromotionRecord:
    defaults = {
        "session_id": "sess-1",
        "source_turn": 5,
        "fact_type": "decision",
        "content": "Chose PostgreSQL over SQLite for persistence",
        "confidence": 0.95,
        "promotion_reason": "High-confidence decision",
    }
    defaults.update(overrides)
    return PromotionRecord(**defaults)


# ---------------------------------------------------------------------------
# Canonical Promotion Model
# ---------------------------------------------------------------------------

class TestPromotionRecord:
    def test_auto_dedupe_key(self):
        rec = _make_promotion()
        assert not rec.dedupe_key
        updated = rec.with_auto_dedupe()
        assert updated.dedupe_key
        # Same input → same key
        assert updated.dedupe_key == rec.with_auto_dedupe().dedupe_key

    def test_dedupe_key_deterministic(self):
        r1 = _make_promotion(session_id="s1", content="foo", fact_type="decision")
        r2 = _make_promotion(session_id="s1", content="foo", fact_type="decision")
        assert r1.with_auto_dedupe().dedupe_key == r2.with_auto_dedupe().dedupe_key

    def test_dedupe_key_differs_for_different_content(self):
        r1 = _make_promotion(content="foo")
        r2 = _make_promotion(content="bar")
        assert r1.with_auto_dedupe().dedupe_key != r2.with_auto_dedupe().dedupe_key

    def test_with_auto_dedupe_idempotent(self):
        rec = _make_promotion()
        first = rec.with_auto_dedupe()
        second = first.with_auto_dedupe()
        assert first.dedupe_key == second.dedupe_key


# ---------------------------------------------------------------------------
# Memory Engine Config
# ---------------------------------------------------------------------------

class TestMemoryEngineConfig:
    def test_defaults(self):
        cfg = MemoryEngineConfig(id="e1", type="generic_http")
        assert cfg.enabled
        assert cfg.priority == 0
        assert cfg.base_url == ""
        assert cfg.api_key_env == ""

    def test_resolved_api_key_missing_env(self):
        cfg = MemoryEngineConfig(id="e1", type="generic_http", api_key_env="NONEXISTENT_KEY_12345")
        assert cfg.resolved_api_key == ""

    def test_extra_config(self):
        cfg = MemoryEngineConfig(id="e1", type="generic_http", extra={"user_id": "alice"})
        assert cfg.extra["user_id"] == "alice"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestMemoryEngineRegistry:
    def test_register_and_lookup(self):
        registry = MemoryEngineRegistry()
        cfg = _make_config()
        registry.register(cfg)
        assert registry.engine_count == 1
        assert registry.get_config("test-engine") is not None
        assert registry.get_config("nonexistent") is None

    def test_default_engine_highest_priority(self):
        registry = MemoryEngineRegistry()
        registry.register(_make_config(id="low", priority=1))
        registry.register(_make_config(id="high", priority=10))
        assert registry.default_engine_id == "high"

    def test_disabled_engine_not_default(self):
        registry = MemoryEngineRegistry()
        registry.register(_make_config(id="off", priority=10, enabled=False))
        assert registry.default_engine_id is None

    def test_list_engines(self):
        registry = MemoryEngineRegistry()
        registry.register(_make_config(id="e1", priority=5))
        registry.register(_make_config(id="e2", priority=10))
        engines = registry.list_engines()
        assert len(engines) == 2
        ids = {e["id"] for e in engines}
        assert ids == {"e1", "e2"}

    def test_get_adapter_disabled_returns_none(self):
        registry = MemoryEngineRegistry()
        registry.register(_make_config(enabled=False))
        assert registry.get_adapter("test-engine") is None

    def test_get_adapter_nonexistent_returns_none(self):
        registry = MemoryEngineRegistry()
        assert registry.get_adapter("nope") is None

    def test_load_from_config(self):
        registry = MemoryEngineRegistry()
        configs = [
            _make_config(id="e1", priority=5),
            _make_config(id="e2", priority=10),
        ]
        registry.load_from_config(configs)
        assert registry.engine_count == 2

    def test_singleton_get_registry(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2


# ---------------------------------------------------------------------------
# Adapter Base
# ---------------------------------------------------------------------------

class StubAdapter(MemoryAdapterBase):
    """In-memory adapter for testing."""

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._healthy = True
        self._memories: dict[str, PromotionRecord] = {}

    async def validate_config(self) -> list[str]:
        return [] if self.config.base_url else ["base_url required"]

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=True,
        )

    async def healthcheck(self) -> bool:
        return self._healthy

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        self._memories[promotion.promotion_id] = promotion
        return PromotionResult(
            promotion_id=promotion.promotion_id,
            engine_id=self.config.id,
            outcome=PromotionOutcome.SUCCESS,
            remote_id=promotion.promotion_id,
        )

    async def dedupe_lookup(self, promotion: PromotionRecord) -> str | None:
        for existing in self._memories.values():
            if existing.dedupe_key and existing.dedupe_key == promotion.dedupe_key:
                return existing.promotion_id
        return None


class FailingAdapter(MemoryAdapterBase):
    """Adapter that always fails promotions."""

    async def validate_config(self) -> list[str]:
        return []

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities()

    async def healthcheck(self) -> bool:
        return False

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        return PromotionResult(
            promotion_id=promotion.promotion_id,
            engine_id=self.config.id,
            outcome=PromotionOutcome.FAILED,
            error_message="Simulated failure",
        )


class TestStubAdapter:
    @pytest.mark.asyncio
    async def test_validate_config(self):
        cfg = _make_config()
        adapter = StubAdapter(cfg)
        problems = await adapter.validate_config()
        assert problems == []

    @pytest.mark.asyncio
    async def test_capabilities(self):
        adapter = StubAdapter(_make_config())
        caps = await adapter.capabilities()
        assert caps.promote_fact
        assert caps.dedupe_lookup

    @pytest.mark.asyncio
    async def test_healthcheck(self):
        adapter = StubAdapter(_make_config())
        assert await adapter.healthcheck()

    @pytest.mark.asyncio
    async def test_promote_fact(self):
        adapter = StubAdapter(_make_config())
        rec = _make_promotion()
        result = await adapter.promote_fact(rec)
        assert result.outcome == PromotionOutcome.SUCCESS
        assert result.remote_id == rec.promotion_id

    @pytest.mark.asyncio
    async def test_promote_batch_default(self):
        adapter = StubAdapter(_make_config())
        records = [_make_promotion(content=f"fact-{i}") for i in range(3)]
        results = await adapter.promote_batch(records)
        assert len(results) == 3
        assert all(r.outcome == PromotionOutcome.SUCCESS for r in results)

    @pytest.mark.asyncio
    async def test_dedupe_lookup(self):
        adapter = StubAdapter(_make_config())
        rec = _make_promotion()
        await adapter.promote_fact(rec.with_auto_dedupe())
        # Same dedupe key should match
        dup = _make_promotion()
        dup = dup.with_auto_dedupe()
        dup.dedupe_key = rec.with_auto_dedupe().dedupe_key
        found = await adapter.dedupe_lookup(dup)
        assert found is not None


# ---------------------------------------------------------------------------
# Promotion Service
# ---------------------------------------------------------------------------

class TestPromotionPolicy:
    def test_explicit_always_promotes(self):
        svc = PromotionService()
        assert svc.should_promote("error", 0.1, explicit=True)

    def test_non_promotable_type_rejected(self):
        svc = PromotionService()
        assert not svc.should_promote("error", 0.99)
        assert not svc.should_promote("tool_result", 0.99)

    def test_low_confidence_rejected(self):
        svc = PromotionService()
        assert not svc.should_promote("decision", 0.5)

    def test_high_confidence_decision_promoted(self):
        svc = PromotionService()
        assert svc.should_promote("decision", 0.95)

    def test_observation_needs_multi_turn(self):
        svc = PromotionService()
        assert not svc.should_promote("observation", 0.95, turn_count=1)
        assert svc.should_promote("observation", 0.95, turn_count=3)

    def test_durable_tag_bypasses_survival(self):
        svc = PromotionService()
        assert svc.should_promote("observation", 0.95, turn_count=1, tags=["durable"])

    def test_promote_tag_bypasses_survival(self):
        svc = PromotionService()
        assert svc.should_promote("state", 0.95, turn_count=1, tags=["promote"])


class TestPromotionService:
    @pytest.mark.asyncio
    async def test_no_engine_skips(self):
        svc = PromotionService(registry=MemoryEngineRegistry())
        rec = _make_promotion()
        result = await svc.promote_fact(rec)
        assert result.outcome == PromotionOutcome.SKIPPED
        assert "No memory engine" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_dry_run_skips(self):
        registry = MemoryEngineRegistry()
        # Manually inject a stub adapter
        cfg = _make_config()
        registry.register(cfg)
        registry._adapters["test-engine"] = StubAdapter(cfg)

        svc = PromotionService(registry=registry)
        result = await svc.promote_fact(_make_promotion(), dry_run=True)
        assert result.outcome == PromotionOutcome.SKIPPED
        assert "Dry run" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_promote_fact_success(self):
        registry = MemoryEngineRegistry()
        cfg = _make_config()
        registry.register(cfg)
        registry._adapters["test-engine"] = StubAdapter(cfg)

        svc = PromotionService(registry=registry)
        result = await svc.promote_fact(_make_promotion())
        assert result.outcome == PromotionOutcome.SUCCESS
        assert svc.stats["succeeded"] == 1

    @pytest.mark.asyncio
    async def test_promote_fact_dedupe_skip(self):
        registry = MemoryEngineRegistry()
        cfg = _make_config()
        registry.register(cfg)
        adapter = StubAdapter(cfg)
        registry.register(cfg)
        registry._adapters["test-engine"] = adapter

        svc = PromotionService(registry=registry)
        rec = _make_promotion().with_auto_dedupe()

        # First promotion succeeds
        r1 = await svc.promote_fact(rec)
        assert r1.outcome == PromotionOutcome.SUCCESS

        # Second with same dedupe key is skipped
        r2 = await svc.promote_fact(rec)
        assert r2.outcome == PromotionOutcome.SKIPPED
        assert "Dedupe" in (r2.error_message or "")

    @pytest.mark.asyncio
    async def test_promote_batch(self):
        registry = MemoryEngineRegistry()
        cfg = _make_config()
        registry.register(cfg)
        registry._adapters["test-engine"] = StubAdapter(cfg)

        svc = PromotionService(registry=registry)
        records = [_make_promotion(content=f"fact-{i}") for i in range(3)]
        results = await svc.promote_batch(records)
        assert len(results) == 3
        assert svc.stats["succeeded"] == 3

    @pytest.mark.asyncio
    async def test_audit_trail(self):
        registry = MemoryEngineRegistry()
        cfg = _make_config()
        registry.register(cfg)
        registry._adapters["test-engine"] = StubAdapter(cfg)

        svc = PromotionService(registry=registry)
        await svc.promote_fact(_make_promotion())
        assert len(svc.audit_trail) == 1

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        svc = PromotionService(registry=MemoryEngineRegistry())
        # Skip counts as attempted + skipped
        await svc.promote_fact(_make_promotion())
        assert svc.stats["attempted"] == 1
        assert svc.stats["skipped"] == 1


# ---------------------------------------------------------------------------
# New Adapter Import + Validation Tests
# ---------------------------------------------------------------------------

class TestBasicMemoryAdapter:
    def test_import(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        assert Adapter is not None

    def test_filesystem_mode_config(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        cfg = _make_config(type="basic_memory", base_url="C:/tmp/vault")
        adapter = Adapter(cfg)
        assert adapter._mode == "filesystem"

    def test_api_mode_config(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        cfg = _make_config(type="basic_memory", base_url="http://localhost:8080", extra={"mode": "api"})
        adapter = Adapter(cfg)
        assert adapter._mode == "api"

    def test_markdown_generation(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        cfg = _make_config(type="basic_memory", base_url="C:/tmp/vault")
        adapter = Adapter(cfg)
        rec = _make_promotion(tags=["durable"], touched_files=["/src/app.py"])
        md = adapter._build_markdown(rec)
        assert "---" in md
        assert "title:" in md
        assert rec.content[:80] in md
        assert "[decision]" in md
        assert "[source] archolith-proxy promotion" in md

    def test_slugify(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        assert Adapter._slugify("Hello World! @#") == "hello-world"
        assert Adapter._slugify("a" * 100) == "a" * 60

    @pytest.mark.asyncio
    async def test_validate_config_no_base_url(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        cfg = MemoryEngineConfig(id="e1", type="basic_memory")
        adapter = Adapter(cfg)
        problems = await adapter.validate_config()
        assert len(problems) > 0

    @pytest.mark.asyncio
    async def test_capabilities(self):
        from archolith_proxy.memory.adapters.basic_memory import Adapter
        cfg = _make_config(type="basic_memory", base_url="C:/tmp/vault")
        adapter = Adapter(cfg)
        caps = await adapter.capabilities()
        assert caps.promote_fact
        assert caps.promote_batch


class TestClaudeMemAdapter:
    def test_import(self):
        from archolith_proxy.memory.adapters.claude_mem import Adapter
        assert Adapter is not None

    @pytest.mark.asyncio
    async def test_validate_config(self):
        from archolith_proxy.memory.adapters.claude_mem import Adapter
        cfg = _make_config(type="claude_mem", base_url="http://localhost:37777")
        adapter = Adapter(cfg)
        problems = await adapter.validate_config()
        assert problems == []

    @pytest.mark.asyncio
    async def test_capabilities(self):
        from archolith_proxy.memory.adapters.claude_mem import Adapter
        cfg = _make_config(type="claude_mem", base_url="http://localhost:37777")
        adapter = Adapter(cfg)
        caps = await adapter.capabilities()
        assert caps.promote_fact
        assert caps.list_by_source

    def test_payload_structure(self):
        from archolith_proxy.memory.adapters.claude_mem import Adapter
        cfg = _make_config(type="claude_mem", base_url="http://localhost:37777")
        adapter = Adapter(cfg)
        rec = _make_promotion()
        payload = adapter._build_payload(rec)
        assert "content" in payload
        assert "type" in payload
        assert "session_id" in payload


class TestCogneeAdapter:
    def test_import(self):
        from archolith_proxy.memory.adapters.cognee import Adapter
        assert Adapter is not None

    @pytest.mark.asyncio
    async def test_validate_config(self):
        from archolith_proxy.memory.adapters.cognee import Adapter
        cfg = _make_config(type="cognee", base_url="http://localhost:8000")
        adapter = Adapter(cfg)
        problems = await adapter.validate_config()
        assert problems == []

    @pytest.mark.asyncio
    async def test_capabilities(self):
        from archolith_proxy.memory.adapters.cognee import Adapter
        cfg = _make_config(type="cognee", base_url="http://localhost:8000")
        adapter = Adapter(cfg)
        caps = await adapter.capabilities()
        assert caps.promote_fact
        assert caps.delete_promoted  # Cognee has `forget`

    def test_dataset_config(self):
        from archolith_proxy.memory.adapters.cognee import Adapter
        cfg = _make_config(type="cognee", base_url="http://localhost:8000", extra={"dataset": "my-data"})
        adapter = Adapter(cfg)
        assert adapter._dataset == "my-data"

    def test_default_dataset(self):
        from archolith_proxy.memory.adapters.cognee import Adapter
        cfg = _make_config(type="cognee", base_url="http://localhost:8000")
        adapter = Adapter(cfg)
        assert adapter._dataset == "archolith-proxy"


class TestOpenMemoryAdapter:
    def test_import(self):
        from archolith_proxy.memory.adapters.openmemory import Adapter
        assert Adapter is not None

    @pytest.mark.asyncio
    async def test_validate_config(self):
        from archolith_proxy.memory.adapters.openmemory import Adapter
        cfg = _make_config(type="openmemory", base_url="http://localhost:8080")
        adapter = Adapter(cfg)
        problems = await adapter.validate_config()
        assert problems == []

    @pytest.mark.asyncio
    async def test_capabilities(self):
        from archolith_proxy.memory.adapters.openmemory import Adapter
        cfg = _make_config(type="openmemory", base_url="http://localhost:8080")
        adapter = Adapter(cfg)
        caps = await adapter.capabilities()
        assert caps.promote_fact
        assert caps.delete_promoted
        assert caps.list_by_source

    def test_user_id_config(self):
        from archolith_proxy.memory.adapters.openmemory import Adapter
        cfg = _make_config(type="openmemory", base_url="http://localhost:8080", extra={"user_id": "alice"})
        adapter = Adapter(cfg)
        assert adapter._user_id == "alice"


class TestNocturneMemoryAdapter:
    def test_import(self):
        from archolith_proxy.memory.adapters.nocturne_memory import Adapter
        assert Adapter is not None

    @pytest.mark.asyncio
    async def test_validate_config(self):
        from archolith_proxy.memory.adapters.nocturne_memory import Adapter
        cfg = _make_config(type="nocturne_memory", base_url="http://localhost:8233")
        adapter = Adapter(cfg)
        problems = await adapter.validate_config()
        assert problems == []

    @pytest.mark.asyncio
    async def test_capabilities(self):
        from archolith_proxy.memory.adapters.nocturne_memory import Adapter
        cfg = _make_config(type="nocturne_memory", base_url="http://localhost:8233")
        adapter = Adapter(cfg)
        caps = await adapter.capabilities()
        assert caps.promote_fact
        assert caps.update_promoted  # Nocturne supports update
        assert caps.delete_promoted
        assert caps.list_by_source

    def test_domain_config(self):
        from archolith_proxy.memory.adapters.nocturne_memory import Adapter
        cfg = _make_config(type="nocturne_memory", base_url="http://localhost:8233", extra={"domain": "work"})
        adapter = Adapter(cfg)
        assert adapter._domain == "work"
        assert adapter._parent_uri == "work://archolith-proxy"

    def test_payload_structure(self):
        from archolith_proxy.memory.adapters.nocturne_memory import Adapter
        cfg = _make_config(type="nocturne_memory", base_url="http://localhost:8233")
        adapter = Adapter(cfg)
        rec = _make_promotion()
        payload = adapter._build_payload(rec)
        assert "parent_path" in payload
        assert "content" in payload
        assert "priority" in payload
        assert "disclosure" in payload


# ---------------------------------------------------------------------------
# Registry adapter type coverage
# ---------------------------------------------------------------------------

class TestRegistryAdapterTypes:
    def test_all_adapter_types_registered(self):
        from archolith_proxy.memory.registry import _ADAPTER_TYPES
        expected = {
            "archolith_memory", "mem0", "zep", "generic_http",
            "basic_memory", "claude_mem", "cognee", "openmemory", "nocturne_memory",
        }
        assert set(_ADAPTER_TYPES.keys()) == expected

    def test_registry_loads_basic_memory_config(self):
        registry = MemoryEngineRegistry()
        cfg = _make_config(id="obsidian", type="basic_memory", base_url="C:/tmp/vault")
        registry.register(cfg)
        assert registry.engine_count == 1
        assert registry.get_config("obsidian") is not None
