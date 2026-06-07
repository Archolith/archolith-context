"""Tests for graph session CRUD Pass 1 fixes (find_by_fingerprint, atomic find_or_create)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from archolith_proxy.graph.ladybug_backend import LadybugBackend


# ---------------------------------------------------------------------------
# Fixture (self-contained: connect + ensure schema + close)
# ---------------------------------------------------------------------------


@pytest.fixture
async def backend_fixture():
    """Provide a connected LadybugBackend with schema, closed after the test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        be = LadybugBackend(db_path=str(Path(tmpdir) / "test.lbug"), max_concurrent_queries=4)
        await be.connect()
        await be.ensure_schema()
        yield be
        await be.close()


# ---------------------------------------------------------------------------
# 1.1: find_session_by_fingerprint returns session when present, None when absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_fingerprint_returns_session_when_present(backend_fixture):
    """find_session_by_fingerprint returns the session dict when fingerprint exists."""
    backend = backend_fixture

    fingerprint = "test-fp-001"
    session_data = await backend.create_session("sess-001", fingerprint=fingerprint)
    assert session_data is not None
    assert session_data.get("session_id") == "sess-001"

    found = await backend.find_session_by_fingerprint(fingerprint)
    assert found is not None
    assert found.get("session_id") == "sess-001"
    assert found.get("fingerprint") == fingerprint


@pytest.mark.asyncio
async def test_find_by_fingerprint_returns_none_when_absent(backend_fixture):
    """find_session_by_fingerprint returns None when fingerprint does not exist."""
    backend = backend_fixture

    found = await backend.find_session_by_fingerprint("nonexistent-fp")
    assert found is None


# ---------------------------------------------------------------------------
# 2.1: atomic find_or_create_by_fingerprint - two calls yield ONE session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_or_create_by_fingerprint_atomic_creates_once(backend_fixture):
    """Two consecutive find_or_create calls with the same fingerprint yield one session."""
    backend = backend_fixture

    fingerprint = "atomic-test-fp-001"

    session1, is_new1 = await backend.find_or_create_by_fingerprint(fingerprint)
    assert is_new1 is True
    session_id_1 = session1.get("session_id")
    assert session_id_1 is not None

    session2, is_new2 = await backend.find_or_create_by_fingerprint(fingerprint)
    assert is_new2 is False
    session_id_2 = session2.get("session_id")

    assert session_id_1 == session_id_2
    assert session2.get("fingerprint") == fingerprint


@pytest.mark.asyncio
async def test_find_or_create_by_fingerprint_different_fingerprints(backend_fixture):
    """Different fingerprints create different sessions."""
    backend = backend_fixture

    session1, is_new1 = await backend.find_or_create_by_fingerprint("fp-001")
    assert is_new1 is True
    sid1 = session1.get("session_id")

    session2, is_new2 = await backend.find_or_create_by_fingerprint("fp-002")
    assert is_new2 is True
    sid2 = session2.get("session_id")

    assert sid1 != sid2
