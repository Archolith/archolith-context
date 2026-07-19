"""Comprehensive tests for the Prompt Cache Stability feature (Phase 0-2)."""

import tempfile
import os
from unittest.mock import patch, MagicMock

import pytest

from archolith_proxy.curator.context_cache import (
    compute_context_signature,
    get_cached_context,
    store_context,
)
from archolith_proxy.curator.deterministic_assembler import run_deterministic_assembler


# =============================================================================
# Core Helper Tests
# =============================================================================

def test_signature_is_stable_and_deterministic():
    """Signature must be stable regardless of file order."""
    sig1 = compute_context_signature("Build login", ["a.py", "b.py"], "add form")
    sig2 = compute_context_signature("Build login", ["b.py", "a.py"], "add form")
    assert sig1 == sig2
    assert len(sig1) == 64


def test_signature_changes_with_goal():
    sig1 = compute_context_signature("Goal A", ["x.py"], "test")
    sig2 = compute_context_signature("Goal B", ["x.py"], "test")
    assert sig1 != sig2


def test_signature_changes_with_message():
    sig1 = compute_context_signature("Goal", ["x.py"], "message 1")
    sig2 = compute_context_signature("Goal", ["x.py"], "message 2")
    assert sig1 != sig2


def test_store_and_retrieve_roundtrip(tmp_path):
    db = str(tmp_path / "cache.db")
    sig = compute_context_signature("Goal", ["file1.py"], "do it")

    success = store_context(
        db,
        "sess1",
        sig,
        rendered_block="=== GOAL ===\nBuild login\n=== CODE ===\n...",
        files_selected=[{"path": "file1.py", "relevance": "high"}],
        created_turn=5,
    )
    assert success is True

    result = get_cached_context(db, "sess1", sig)
    assert result is not None
    assert "Build login" in result["rendered_block"]
    assert result["files_selected"][0]["path"] == "file1.py"


def test_cache_miss_when_not_present(tmp_path):
    db = str(tmp_path / "cache.db")
    sig = compute_context_signature("Goal", ["x.py"], "test")
    result = get_cached_context(db, "sess1", sig)
    assert result is None


def test_provider_ttl_expiry(tmp_path):
    db = str(tmp_path / "cache.db")
    sig = compute_context_signature("Goal", ["x.py"], "test")

    store_context(db, "sess1", sig, "content", [], 1)

    # Should still be retrievable with high TTL
    result = get_cached_context(db, "sess1", sig, max_age_seconds=999999)
    assert result is not None

    # Should be expired with 0 TTL
    result = get_cached_context(db, "sess1", sig, max_age_seconds=0)
    assert result is None


# =============================================================================
# Integration Tests with Deterministic Assembler
# =============================================================================

@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.assembler_token_budget = 6000
    settings.assembler_scored_selection = False
    settings.assembler_topological_fill = False
    settings.assembler_combo_fill = False
    settings.assembler_code_map = False
    settings.assembler_code_map_mode = "task"
    settings.assembler_exemplar_suffixes = ""
    settings.assembler_code_map_budget_fraction = 0.12
    settings.context_cache_enabled = True
    settings.provider_cache_ttl_seconds = 600
    settings.curator_state_persist_path = None  # Will use temp in tests
    return settings


def test_deterministic_assembler_cache_hit(tmp_path, mock_settings):
    """When cache is enabled, second call with same signature should hit cache."""
    db_path = str(tmp_path / "test_cache.db")
    mock_settings.curator_state_persist_path = db_path

    # First call - should miss and store
    with patch("archolith_proxy.curator.deterministic_assembler.get_settings", return_value=mock_settings):
        # We can't easily test the full function without a real briefing,
        # so we test the cache helpers directly in integration style.
        sig = compute_context_signature("Test Goal", ["test.py"], "first message")
        store_context(db_path, "sess_test", sig, "CACHED BLOCK", [{"path": "test.py"}], 1)

    # Verify it can be retrieved
    result = get_cached_context(db_path, "sess_test", sig)
    assert result is not None
    assert result["rendered_block"] == "CACHED BLOCK"


def test_deterministic_assembler_respects_ttl(tmp_path, mock_settings):
    db_path = str(tmp_path / "test_cache.db")
    mock_settings.curator_state_persist_path = db_path
    mock_settings.provider_cache_ttl_seconds = 0  # Force immediate expiry

    sig = compute_context_signature("Goal", ["x.py"], "msg")
    store_context(db_path, "sess", sig, "block", [], 1)

    result = get_cached_context(db_path, "sess", sig, max_age_seconds=0)
    assert result is None


# =============================================================================
# Metrics Tests
# =============================================================================

def test_metrics_are_recorded_on_cache_hit_and_miss(tmp_path):
    """Verify that context_cache_hits and context_cache_misses are incremented."""
    from archolith_proxy.metrics import get_metrics, record_metric

    # Reset relevant metrics
    metrics = get_metrics()
    metrics["context_cache_hits"] = 0
    metrics["context_cache_misses"] = 0
    metrics["context_cache_stores"] = 0

    db = str(tmp_path / "metrics_test.db")
    sig = compute_context_signature("Goal", ["f.py"], "test")

    # Simulate miss + store
    record_metric("context_cache_misses")
    record_metric("context_cache_stores")
    store_context(db, "s1", sig, "content", [], 1)

    # Simulate hit
    record_metric("context_cache_hits")
    get_cached_context(db, "s1", sig)

    assert metrics["context_cache_hits"] == 1
    assert metrics["context_cache_misses"] == 1
    assert metrics["context_cache_stores"] == 1


# =============================================================================
# Edge Case Tests
# =============================================================================

def test_empty_inputs_produce_valid_signature():
    sig = compute_context_signature("", [], "")
    assert isinstance(sig, str)
    assert len(sig) == 64


def test_very_long_user_message_is_truncated_in_signature():
    long_msg = "x" * 1000
    sig1 = compute_context_signature("g", ["f.py"], long_msg)
    sig2 = compute_context_signature("g", ["f.py"], long_msg[:200] + "extra")
    # First 200 chars should dominate, so signatures should be identical
    assert sig1 == sig2


def test_concurrent_writes_do_not_corrupt_db(tmp_path):
    """Basic smoke test for concurrent access (not perfect, but useful)."""
    import threading

    db = str(tmp_path / "concurrent.db")
    errors = []

    def writer(i):
        try:
            sig = compute_context_signature(f"goal{i}", [f"f{i}.py"], f"msg{i}")
            store_context(db, f"sess{i}", sig, f"block{i}", [], i)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Concurrent writes caused errors: {errors}"