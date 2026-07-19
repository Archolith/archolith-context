"""Basic tests for the context cache (Phase 0/1)."""

import tempfile
import os

from archolith_proxy.curator.context_cache import (
    compute_context_signature,
    get_cached_context,
    store_context,
)


def test_signature_is_stable():
    sig1 = compute_context_signature("Build login", ["a.py", "b.py"], "add form")
    sig2 = compute_context_signature("Build login", ["b.py", "a.py"], "add form")
    assert sig1 == sig2
    assert len(sig1) == 64


def test_store_and_retrieve(tmp_path):
    db = str(tmp_path / "test_cache.db")

    sig = compute_context_signature("Goal", ["file1.py"], "do it")
    store_context(
        db,
        "sess1",
        sig,
        rendered_block="=== GOAL ===\nBuild login",
        files_selected=[{"path": "file1.py"}],
        created_turn=5,
    )

    result = get_cached_context(db, "sess1", sig)
    assert result is not None
    assert "Build login" in result["rendered_block"]
    assert result["files_selected"][0]["path"] == "file1.py"


def test_cache_miss_when_not_present(tmp_path):
    db = str(tmp_path / "test_cache.db")
    sig = compute_context_signature("Goal", ["x.py"], "test")
    result = get_cached_context(db, "sess1", sig)
    assert result is None


def test_provider_ttl_expiry(tmp_path):
    db = str(tmp_path / "test_cache.db")
    sig = compute_context_signature("Goal", ["x.py"], "test")

    store_context(db, "sess1", sig, "content", [], 1)

    # Should still be there with very high TTL
    result = get_cached_context(db, "sess1", sig, max_age_seconds=999999)
    assert result is not None

    # Should be gone with 0 TTL
    result = get_cached_context(db, "sess1", sig, max_age_seconds=0)
    assert result is None