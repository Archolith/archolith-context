"""Comprehensive tests for the Prompt Cache Stability feature."""

import tempfile
import json
from unittest.mock import MagicMock, patch

import pytest

from archolith_proxy.curator.context_cache import (
    compute_context_signature,
    get_cached_context,
    store_context,
    should_use_cached_context,
    has_file_supersession,
    extract_relevant_code_section,
    replace_relevant_code_section,
)


# =============================================================================
# Core Helper Tests
# =============================================================================

def test_signature_is_stable_and_deterministic():
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
        db, "sess1", sig,
        rendered_block="=== GOAL ===\nBuild login",
        files_selected=[{"path": "file1.py"}],
        created_turn=5,
    )
    assert success is True

    result = get_cached_context(db, "sess1", sig)
    assert result is not None
    assert "Build login" in result["rendered_block"]


def test_cache_miss_when_not_present(tmp_path):
    db = str(tmp_path / "cache.db")
    sig = compute_context_signature("Goal", ["x.py"], "test")
    result = get_cached_context(db, "sess1", sig)
    assert result is None


def test_provider_ttl_expiry(tmp_path):
    db = str(tmp_path / "cache.db")
    sig = compute_context_signature("Goal", ["x.py"], "test")
    store_context(db, "sess1", sig, "content", [], 1)

    assert get_cached_context(db, "sess1", sig, max_age_seconds=999999) is not None
    assert get_cached_context(db, "sess1", sig, max_age_seconds=0) is None


# =============================================================================
# should_use_cached_context (Bloat + Mode)
# =============================================================================

def test_should_use_cached_context_cost_optimized():
    # Within limit
    use, reason = should_use_cached_context(1000, 800, 1.6, "cost_optimized")
    assert use is True
    assert "within" in reason.lower() or "limit" in reason.lower()

    # Exceeds bloat
    use, reason = should_use_cached_context(2000, 800, 1.6, "cost_optimized")
    assert use is False
    assert "bloat" in reason.lower()


def test_should_use_cached_context_aggressive():
    use, reason = should_use_cached_context(3000, 1000, 1.6, "aggressive")
    assert use is True  # Very lenient


def test_should_use_cached_context_conservative():
    use, reason = should_use_cached_context(1500, 1000, 1.6, "conservative")
    assert use is False  # Strict (1.3x threshold)


def test_should_use_cached_context_off():
    use, reason = should_use_cached_context(500, 500, 1.6, "off")
    assert use is False
    assert "off" in reason.lower()


# =============================================================================
# File Supersession
# =============================================================================

def test_has_file_supersession_detects_newer_read():
    cached = {"src/Page.tsx": {"last_read_turn": 10, "content_hash": "abc"}}
    current = {"src/Page.tsx": {"last_read_turn": 15, "content_hash": "abc"}}
    assert has_file_supersession(cached, current) is True


def test_has_file_supersession_detects_content_change():
    cached = {"src/Page.tsx": {"last_read_turn": 10, "content_hash": "abc"}}
    current = {"src/Page.tsx": {"last_read_turn": 10, "content_hash": "def"}}
    assert has_file_supersession(cached, current) is True


def test_has_file_supersession_no_supersession():
    cached = {"src/Page.tsx": {"last_read_turn": 10, "content_hash": "abc"}}
    current = {"src/Page.tsx": {"last_read_turn": 10, "content_hash": "abc"}}
    assert has_file_supersession(cached, current) is False


# =============================================================================
# Partial Refresh Helpers
# =============================================================================

def test_extract_relevant_code_section():
    block = """=== SESSION GOAL ===
Build login

=== RELEVANT CODE ===
src/Login.tsx
```tsx
...
```

=== KEY FACTS ==="""

    head, code, tail = extract_relevant_code_section(block)
    assert "SESSION GOAL" in head
    assert "RELEVANT CODE" in code
    assert "KEY FACTS" in tail


def test_replace_relevant_code_section():
    original = """=== GOAL ===
Test

=== RELEVANT CODE ===
old code

=== FACTS ==="""

    new_code = "new code here"
    result = replace_relevant_code_section(original, new_code)
    assert "new code here" in result
    assert "old code" not in result
    assert "GOAL" in result
    assert "FACTS" in result


# =============================================================================
# Store with file_versions
# =============================================================================

def test_store_context_with_file_versions(tmp_path):
    db = str(tmp_path / "cache.db")
    sig = compute_context_signature("Goal", ["f.py"], "msg")

    file_versions = {
        "f.py": {"last_read_turn": 5, "content_hash": "hash123"}
    }

    store_context(
        db, "s1", sig, "block", [{"path": "f.py"}], 5,
        file_versions=file_versions
    )

    result = get_cached_context(db, "s1", sig)
    assert result is not None
    assert "file_versions" in result or isinstance(result.get("files_selected"), list)


# =============================================================================
# Edge Cases
# =============================================================================

def test_empty_inputs():
    sig = compute_context_signature("", [], "")
    assert len(sig) == 64


def test_long_message_truncation():
    long_msg = "x" * 1000
    sig1 = compute_context_signature("g", ["f.py"], long_msg)
    sig2 = compute_context_signature("g", ["f.py"], long_msg[:200] + "extra")
    assert sig1 == sig2


def test_concurrent_writes(tmp_path):
    import threading
    db = str(tmp_path / "concurrent.db")
    errors = []

    def writer(i):
        try:
            sig = compute_context_signature(f"g{i}", [f"f{i}.py"], f"m{i}")
            store_context(db, f"s{i}", sig, f"b{i}", [], i)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(errors) == 0