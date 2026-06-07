"""Unit tests for per-session turn locking."""

import asyncio

import pytest

from archolith_proxy.proxy.locks import (
    get_session_lock,
    is_extraction_pending,
    wait_for_prior_extraction,
    cleanup_session_lock,
    cleanup_stale_locks,
    _reset_locks,
)


@pytest.fixture(autouse=True)
def reset_locks_before_test():
    """Reset locks before each test for isolation."""
    _reset_locks()
    yield
    _reset_locks()


class TestGetSessionLock:
    def test_returns_lock_for_new_session(self):
        lock = get_session_lock("test-session-1")
        assert isinstance(lock, asyncio.Lock)
        assert not lock.locked()

    def test_returns_same_lock_for_same_session(self):
        lock1 = get_session_lock("test-session-2")
        lock2 = get_session_lock("test-session-2")
        assert lock1 is lock2

    def test_returns_different_locks_for_different_sessions(self):
        lock1 = get_session_lock("session-A")
        lock2 = get_session_lock("session-B")
        assert lock1 is not lock2


class TestWaitForPriorExtraction:
    @pytest.mark.asyncio
    async def test_returns_true_when_no_lock_held(self):
        result = await wait_for_prior_extraction("no-lock-session", timeout_s=0.5)
        assert result is True

    @pytest.mark.asyncio
    async def test_waits_for_held_lock(self):
        lock = get_session_lock("held-session")
        await lock.acquire()
        # Start wait_for in a task
        task = asyncio.create_task(
            wait_for_prior_extraction("held-session", timeout_s=2.0)
        )
        # Give it a moment to start waiting
        await asyncio.sleep(0.1)
        assert not task.done()
        # Release the lock
        lock.release()
        result = await task
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        lock = get_session_lock("timeout-session")
        await lock.acquire()
        try:
            result = await wait_for_prior_extraction("timeout-session", timeout_s=0.1)
            assert result is False
        finally:
            lock.release()


class TestIsExtractionPending:
    @pytest.mark.asyncio
    async def test_false_when_no_lock_held(self):
        assert is_extraction_pending("pending-false-session") is False

    @pytest.mark.asyncio
    async def test_true_when_lock_held(self):
        lock = get_session_lock("pending-true-session")
        await lock.acquire()
        try:
            assert is_extraction_pending("pending-true-session") is True
        finally:
            lock.release()


class TestCleanupSessionLock:
    def test_removes_lock(self):
        lock = get_session_lock("cleanup-session")
        cleanup_session_lock("cleanup-session")
        # After cleanup, a new call should create a different lock
        lock2 = get_session_lock("cleanup-session")
        assert lock is not lock2

    def test_no_error_for_missing_session(self):
        cleanup_session_lock("nonexistent-session")


class TestCleanupStaleLocks:
    def test_no_cleanup_when_under_limit(self):
        # Clean up test locks first
        from archolith_proxy.proxy import locks
        locks._session_locks.clear()
        for i in range(5):
            get_session_lock(f"stale-test-{i}")
        removed = cleanup_stale_locks(max_locks=100)
        assert removed == 0

    def test_removes_oldest_when_over_limit(self):
        from archolith_proxy.proxy import locks
        locks._session_locks.clear()
        for i in range(20):
            get_session_lock(f"stale-over-{i}")
        removed = cleanup_stale_locks(max_locks=10)
        assert removed > 0
        assert len(locks._session_locks) < 20
