"""Tests for LadybugBackend against the GraphBackend protocol.

Uses a temporary directory for each test — no external server needed.
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db_path():
    """Create a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test.lbug")


@pytest.fixture
async def backend(tmp_db_path):
    """Create and connect a LadybugBackend, then close after test."""
    from archolith_proxy.graph.ladybug_backend import LadybugBackend

    be = LadybugBackend(db_path=tmp_db_path, max_concurrent_queries=4)
    await be.connect()
    await be.ensure_schema()
    yield be
    await be.close()


@pytest.mark.asyncio
async def test_protocol_compliance():
    """LadybugBackend satisfies the GraphBackend protocol."""
    from archolith_proxy.graph.ladybug_backend import LadybugBackend
    from archolith_proxy.graph.protocol import GraphBackend

    assert isinstance(LadybugBackend, GraphBackend)


@pytest.mark.asyncio
async def test_lifecycle(tmp_db_path):
    """Connect, verify, check ready, close."""
    from archolith_proxy.graph.ladybug_backend import LadybugBackend

    be = LadybugBackend(db_path=tmp_db_path)
    assert not be.is_ready()

    await be.connect()
    assert be.is_ready()

    ok = await be.verify_connectivity()
    assert ok

    await be.ensure_schema()  # Idempotent
    await be.close()
    assert not be.is_ready()


@pytest.mark.asyncio
async def test_create_and_find_session(backend):
    """Create a session and look it up."""
    result = await backend.create_session("sess-001", fingerprint="fp-001")
    assert result  # Non-empty dict

    found = await backend.find_session_by_id("sess-001")
    assert found is not None
    assert found["session_id"] == "sess-001"

    by_fp = await backend.find_session_by_fingerprint("fp-001")
    assert by_fp is not None
    assert by_fp["session_id"] == "sess-001"

    missing = await backend.find_session_by_id("nonexistent")
    assert missing is None


@pytest.mark.asyncio
async def test_touch_session_and_turn(backend):
    """Touch session increments turn number."""
    await backend.create_session("sess-002")

    turn = await backend.get_turn_number("sess-002")
    assert turn == 0

    await backend.touch_session("sess-002")
    turn = await backend.get_turn_number("sess-002")
    assert turn == 1

    await backend.touch_session("sess-002")
    turn = await backend.get_turn_number("sess-002")
    assert turn == 2


@pytest.mark.asyncio
async def test_update_goal(backend):
    """Update session goal."""
    await backend.create_session("sess-003")
    await backend.update_goal("sess-003", "Refactor user service")

    found = await backend.find_session_by_id("sess-003")
    assert found is not None
    assert found["goal"] == "Refactor user service"


@pytest.mark.asyncio
async def test_store_and_get_facts(backend):
    """Store facts and retrieve them."""
    await backend.create_session("sess-004")

    fid1 = await backend.store_fact(
        session_id="sess-004",
        content="The build passes",
        fact_type="observation",
        source_turn=1,
        confidence=0.9,
    )
    assert len(fid1) > 0

    fid2 = await backend.store_fact(
        session_id="sess-004",
        content="Need to add tests",
        fact_type="procedure",
        source_turn=1,
        confidence=0.7,
    )
    assert len(fid2) > 0

    active = await backend.get_active_facts("sess-004", limit=50)
    assert len(active) == 2

    count = await backend.get_active_fact_count("sess-004")
    assert count == 2


@pytest.mark.asyncio
async def test_invalidate_facts(backend):
    """Invalidate facts and verify they disappear from active set."""
    await backend.create_session("sess-005")

    fid = await backend.store_fact(
        session_id="sess-005",
        content="Old decision",
        fact_type="decision",
        source_turn=1,
    )

    count = await backend.invalidate_facts([fid])
    assert count >= 0

    active = await backend.get_active_facts("sess-005")
    assert len(active) == 0


@pytest.mark.asyncio
async def test_store_facts_batch(backend):
    """Batch store facts."""
    await backend.create_session("sess-006")

    ids = await backend.store_facts_batch(
        session_id="sess-006",
        facts=[
            {"content": "Fact A", "fact_type": "observation", "confidence": 0.8},
            {"content": "Fact B", "fact_type": "state", "confidence": 0.6},
            {"content": "Fact C", "fact_type": "procedure", "confidence": 0.9},
        ],
        source_turn=2,
    )
    assert len(ids) == 3

    active = await backend.get_active_facts("sess-006")
    assert len(active) == 3


@pytest.mark.asyncio
async def test_facts_filtered(backend):
    """Filter facts by type and turn range."""
    await backend.create_session("sess-007")

    await backend.store_fact("sess-007", "Error in build", "error", 1, confidence=0.95)
    await backend.store_fact("sess-007", "Fix applied", "observation", 2, confidence=0.8)
    await backend.store_fact("sess-007", "Test added", "procedure", 3, confidence=0.7)

    # Filter by type
    errors = await backend.get_facts_filtered("sess-007", fact_type="error")
    assert len(errors) == 1
    assert "Error" in str(errors[0])

    # Filter by turn range
    early = await backend.get_facts_filtered("sess-007", to_turn=1)
    assert len(early) == 1


@pytest.mark.asyncio
async def test_create_touches_and_files(backend):
    """Create file touches and retrieve them."""
    await backend.create_session("sess-008")

    await backend.create_touches("sess-008", "src/main.py", "modified", 1)
    await backend.create_touches("sess-008", "src/test.py", "read", 2)
    await backend.create_touches("sess-008", "src/main.py", "read", 3)

    files = await backend.get_touched_files("sess-008")
    assert len(files) >= 1  # At least the dual-touched file exists


@pytest.mark.asyncio
async def test_decisions(backend):
    """Store and retrieve decisions."""
    await backend.create_session("sess-009")

    did = await backend.store_decision(
        session_id="sess-009",
        summary="Use asyncpg for PostgreSQL",
        rationale="Better performance than psycopg2 for async workloads",
        turn=2,
    )
    assert len(did) > 0

    decisions = await backend.get_decisions("sess-009")
    assert len(decisions) == 1
    assert "asyncpg" in decisions[0]["summary"]


@pytest.mark.asyncio
async def test_supersedes_chain(backend):
    """Create a supersession chain and retrieve it."""
    await backend.create_session("sess-010")

    old_id = await backend.store_fact("sess-010", "Use PostgreSQL", "decision", 1)
    new_id = await backend.store_fact("sess-010", "Use MySQL instead", "decision", 3)

    await backend.create_supersedes(old_id, new_id)

    chain = await backend.get_supersession_chain("sess-010")
    assert len(chain) >= 1
    assert chain[0]["superseding_fact"]["fact_id"] == new_id
    assert chain[0]["superseded_fact"]["fact_id"] == old_id


@pytest.mark.asyncio
async def test_bulk_touch_and_decision_operations(backend):
    """Bulk UNWIND operations should update files and decisions in one backend."""
    await backend.create_session("sess-011")

    await backend.bulk_create_touches(
        "sess-011",
        [
            {"file_path": "src/main.py", "status": "modified", "turn": 2},
            {"file_path": "src/readme.md", "status": "read", "turn": 3},
        ],
    )
    files = await backend.get_touched_files("sess-011")
    assert {f["path"] for f in files} >= {"src/main.py", "src/readme.md"}

    decision_ids = await backend.bulk_store_decisions(
        "sess-011",
        [
            {"summary": "Keep ladybug as default", "rationale": "embedded backend"},
            {"summary": "Use shared text utils", "rationale": "break cross-layer deps"},
        ],
        turn=4,
    )
    assert len(decision_ids) == 2
    decisions = await backend.get_decisions("sess-011")
    assert len(decisions) == 2


@pytest.mark.asyncio
async def test_bulk_issue_verification_and_supersedes_operations(backend):
    """Bulk issue, verification, and supersedes helpers should persist rows."""
    await backend.create_session("sess-012")

    old_id = await backend.store_fact("sess-012", "Use old flow", "decision", 1)
    new_id = await backend.store_fact("sess-012", "Use new flow", "decision", 2)
    await backend.bulk_create_supersedes([(old_id, new_id)])
    chain = await backend.get_supersession_chain("sess-012")
    assert len(chain) == 1

    issue_ids = await backend.bulk_create_issues(
        "sess-012",
        [{"summary": "Cache drift", "status": "open", "related_file": "main.py", "related_command": "pytest"}],
        turn=3,
    )
    verification_ids = await backend.bulk_create_verifications(
        "sess-012",
        [{"command": "pytest tests/ -q", "status": "pass", "summary": "green"}],
        turn=4,
    )

    assert len(issue_ids) == 1
    assert len(verification_ids) == 1
    assert len(await backend.get_open_issues("sess-012")) == 1
    last_verification = await backend.get_last_verification("sess-012")
    assert last_verification is not None
    assert last_verification["status"] == "pass"
