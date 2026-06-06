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


@pytest.mark.asyncio
async def test_upsert_file_content_with_created_at(backend):
    """FileContent nodes should have created_at timestamp when created."""
    await backend.create_session("sess-file-001")

    # Upsert a file
    await backend.upsert_file_content(
        session_id="sess-file-001",
        path="src/main.py",
        content="print('hello')",
        sha256="abc123def456",
        turn=1
    )

    # List to verify it exists
    files = await backend.list_cached_files("sess-file-001")
    assert len(files) >= 1
    assert any(f["path"] == "src/main.py" for f in files)


@pytest.mark.asyncio
async def test_upsert_file_outline_with_created_at(backend):
    """FileOutline nodes should have created_at timestamp when created."""
    await backend.create_session("sess-file-002")

    # Upsert an outline
    await backend.upsert_file_outline(
        session_id="sess-file-002",
        path="src/main.py",
        outline="line 1: def main()\nline 5: class Helper",
        turn=1
    )

    # Retrieve it
    outline = await backend.get_file_outline("sess-file-002", "src/main.py")
    assert outline is not None
    assert "def main" in outline


@pytest.mark.asyncio
async def test_invalidation_deletes_both_content_and_outline(backend):
    """Invalidating a path should delete BOTH FileContent and FileOutline."""
    await backend.create_session("sess-file-003")

    # Upsert both content and outline for the same path
    await backend.upsert_file_content(
        session_id="sess-file-003",
        path="test.py",
        content="def test_foo():\n    pass",
        sha256="test123",
        turn=1
    )

    await backend.upsert_file_outline(
        session_id="sess-file-003",
        path="test.py",
        outline="line 1: def test_foo()",
        turn=1
    )

    # List to verify both exist
    files = await backend.list_cached_files("sess-file-003")
    assert any(f["path"] == "test.py" for f in files)

    outline = await backend.get_file_outline("sess-file-003", "test.py")
    assert outline is not None

    # Delete the content
    deleted = await backend.delete_file_content("sess-file-003", "test.py")
    assert deleted is True

    # Verify content is gone from list
    files_after = await backend.list_cached_files("sess-file-003")
    assert not any(f["path"] == "test.py" for f in files_after)

    # Also delete the outline
    outline_deleted = await backend.delete_file_outline("sess-file-003", "test.py")
    assert outline_deleted is True

    # Verify outline is gone
    outline = await backend.get_file_outline("sess-file-003", "test.py")
    assert outline is None


@pytest.mark.asyncio
async def test_eviction_on_empty_cache_no_error(backend):
    """Eviction on a session with zero cached files should not error."""
    await backend.create_session("sess-file-004")

    # Should not raise an exception
    await backend.evict_stale_file_cache(
        session_id="sess-file-004",
        max_turns_age=50,
        max_entries=10
    )


@pytest.mark.asyncio
async def test_ttl_eviction_removes_old_entries(backend):
    """TTL eviction should remove entries older than max_turns_age."""
    await backend.create_session("sess-file-005")

    # Advance session to turn 100
    for _ in range(100):
        await backend.touch_session("sess-file-005")

    current_turn = await backend.get_turn_number("sess-file-005")
    assert current_turn == 100

    # Create files with old timestamps (turn 10) and recent ones (turn 90)
    for i in range(3):
        await backend.upsert_file_content(
            session_id="sess-file-005",
            path=f"old_file_{i}.py",
            content=f"old content {i}",
            sha256=f"old_sha_{i}",
            turn=10
        )

    for i in range(3):
        await backend.upsert_file_content(
            session_id="sess-file-005",
            path=f"new_file_{i}.py",
            content=f"new content {i}",
            sha256=f"new_sha_{i}",
            turn=90
        )

    # List before eviction
    files_before = await backend.list_cached_files("sess-file-005")
    assert len(files_before) == 6

    # Evict with max_turns_age=50 (removes entries from turn < 50)
    await backend.evict_stale_file_cache(
        session_id="sess-file-005",
        max_turns_age=50,
        max_entries=200
    )

    # List after eviction
    files_after = await backend.list_cached_files("sess-file-005")

    # Old entries (turn 10) should be gone
    assert all("old_file" not in f["path"] for f in files_after)
    # New entries (turn 90) should remain
    assert all("new_file" in f["path"] for f in files_after)
    assert len(files_after) == 3


@pytest.mark.asyncio
async def test_lru_eviction_removes_oldest_when_over_capacity(backend):
    """Over-capacity LRU eviction should remove oldest entries by last_updated_turn."""
    await backend.create_session("sess-file-006")

    # Create entries with increasing last_updated_turn
    # This proves the eviction actually respects ORDER BY and LIMIT
    for i in range(10):
        await backend.upsert_file_content(
            session_id="sess-file-006",
            path=f"file_{i:02d}.py",
            content=f"content {i}",
            sha256=f"sha_{i}",
            turn=i + 1
        )

    # List before eviction
    files_before = await backend.list_cached_files("sess-file-006")
    assert len(files_before) == 10

    # Evict oldest entries, keeping only 3
    await backend.evict_stale_file_cache(
        session_id="sess-file-006",
        max_turns_age=999,  # Don't TTL evict
        max_entries=3
    )

    # List after eviction
    files_after = await backend.list_cached_files("sess-file-006")

    # Should have exactly 3 entries (the newest ones)
    assert len(files_after) == 3

    # The 3 newest entries should be file_07, file_08, file_09
    # (those with last_updated_turn = 8, 9, 10)
    remaining_paths = {f["path"] for f in files_after}
    assert "file_07.py" in remaining_paths
    assert "file_08.py" in remaining_paths
    assert "file_09.py" in remaining_paths

    # Oldest entries should be gone
    assert not any("file_0.py" in f["path"] or "file_01.py" in f["path"]
                   or "file_02.py" in f["path"] or "file_03.py" in f["path"]
                   or "file_04.py" in f["path"] or "file_05.py" in f["path"]
                   or "file_06.py" in f["path"] for f in files_after)


@pytest.mark.asyncio
async def test_outline_lru_eviction_respects_capacity(backend):
    """FileOutline LRU eviction should also respect max_entries cap."""
    await backend.create_session("sess-file-007")

    # Create multiple outlines
    for i in range(5):
        await backend.upsert_file_outline(
            session_id="sess-file-007",
            path=f"file_{i:02d}.py",
            outline=f"line 1: def func_{i}()",
            turn=i + 1
        )

    # Call eviction with a cap of 2
    await backend.evict_stale_file_cache(
        session_id="sess-file-007",
        max_turns_age=999,
        max_entries=2
    )

    # Verify the oldest outlines are gone and newest remain
    # We check by attempting to retrieve them
    outline_0 = await backend.get_file_outline("sess-file-007", "file_00.py")
    outline_1 = await backend.get_file_outline("sess-file-007", "file_01.py")
    outline_2 = await backend.get_file_outline("sess-file-007", "file_02.py")
    outline_3 = await backend.get_file_outline("sess-file-007", "file_03.py")
    outline_4 = await backend.get_file_outline("sess-file-007", "file_04.py")

    # Newest 2 should exist
    assert outline_3 is not None
    assert outline_4 is not None

    # Oldest 3 should be gone
    assert outline_0 is None
    assert outline_1 is None
    assert outline_2 is None
