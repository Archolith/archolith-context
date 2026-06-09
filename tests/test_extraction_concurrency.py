"""Concurrency correctness tests — defects #1 and #2 from 2026-06-09 audit.

  Defect #1 (extraction.py): TimeoutError on lock.acquire() falls through into
    the write block (fail-open). Fix: add `return` after the warning.

  Defect #2 (ladybug_sessions.py): concurrent find_or_create_by_fingerprint
    callers can both observe no session and each create one. Fix: per-fingerprint
    asyncio.Lock with double-checked create.

Run before fixes to confirm both FAIL, then apply fixes and confirm both PASS.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from archolith_proxy.openai.extraction import _run_extraction


# ---------------------------------------------------------------------------
# Shared fixture: in-memory LadybugBackend
# ---------------------------------------------------------------------------


@pytest.fixture
async def backend_fixture():
    """Provide a connected LadybugBackend with schema, closed after the test."""
    from archolith_proxy.graph.ladybug_backend import LadybugBackend

    with tempfile.TemporaryDirectory() as tmpdir:
        be = LadybugBackend(db_path=str(Path(tmpdir) / "test.lbug"), max_concurrent_queries=4)
        await be.connect()
        await be.ensure_schema()
        yield be
        await be.close()


# ---------------------------------------------------------------------------
# Test 1: extraction lock timeout must fail closed (return, no writes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_skips_when_lock_timeout():
    """When lock.acquire() times out, _run_extraction must return without writing.

    Before fix: TimeoutError is caught but execution falls through into the write
    block — extract_facts IS called.
    After fix: `return` is added after the warning — extract_facts is NOT called.
    """
    mock_client = AsyncMock()
    mock_extract = AsyncMock(return_value=None)

    with (
        patch.object(asyncio, "wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError)),
        patch("archolith_proxy.proxy.locks.get_session_lock"),
        patch("archolith_proxy.openai.extraction.extract_facts", new=mock_extract),
        patch("archolith_proxy.openai.extraction.extract_facts_per_tool", new=AsyncMock(return_value=None)),
    ):
        await _run_extraction(
            client=mock_client,
            session_id="test-timeout-session",
            turn_number=1,
            messages=[{"role": "user", "content": "hello"}],
            response_text="hello response",
        )

    mock_extract.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: concurrent find_or_create_by_fingerprint must not duplicate sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_or_create_concurrent_same_fingerprint(backend_fixture):
    """Two concurrent calls with the same fingerprint must yield exactly one session.

    Before fix: both coroutines observe no existing session and both create one —
    two Session nodes end up in the DB for the same fingerprint.
    After fix: per-fingerprint lock serializes the create path — exactly one session
    is created regardless of interleaving.
    """
    backend = backend_fixture
    fingerprint = "concurrent-fp-race-001"

    results = await asyncio.gather(
        backend.find_or_create_by_fingerprint(fingerprint),
        backend.find_or_create_by_fingerprint(fingerprint),
    )
    (session1, _), (session2, _) = results

    assert session1.get("session_id") == session2.get("session_id"), (
        f"Race condition: got two different session IDs — "
        f"{session1.get('session_id')!r} and {session2.get('session_id')!r}"
    )

    rows = await backend._execute(
        "MATCH (s:Session {fingerprint: $fp}) RETURN count(s) AS cnt",
        {"fp": fingerprint},
    )
    count = rows[0]["cnt"] if rows else 0
    assert count == 1, f"Race condition: expected 1 session for fingerprint, found {count}"
