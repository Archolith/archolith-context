"""Unit tests for prefetch_file workspace restriction security feature.

Tests cover:
1. workspace-set ALLOW: harness_env has working_directory, prefetch inside it succeeds
2. workspace-set DENY: harness_env has working_directory, prefetch outside it fails
3. no-harness-env DENY: no harness_env metadata, returns denial message
4. explicit-roots PRECEDENCE: prefetch_allowed_roots overrides workspace restriction
5. opt-out: restrict flag False, no explicit roots, no harness_env -> unrestricted read succeeds
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Bootstrap: inject a stub for the 'openai' top-level package so the
# curator/__init__.py import doesn't shadow the installed package.
# ---------------------------------------------------------------------------

def _ensure_openai_stub() -> None:
    """Inject a minimal openai stub into sys.modules if needed."""
    if "openai" not in sys.modules or not hasattr(sys.modules["openai"], "AsyncOpenAI"):
        stub = types.ModuleType("openai")
        stub.AsyncOpenAI = MagicMock()
        stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
        stub.APITimeoutError = type("APITimeoutError", (Exception,), {})
        stub.InternalServerError = type("InternalServerError", (Exception,), {})
        stub.RateLimitError = type("RateLimitError", (Exception,), {})
        sys.modules["openai"] = stub


_ensure_openai_stub()

# Now safe to import curator tools
from archolith_proxy.curator.tools import prefetch_file  # noqa: E402


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestPrefetchWorkspaceRestriction:
    """Test prefetch_file workspace restriction feature."""

    @pytest.mark.asyncio
    async def test_workspace_set_allow(self, tmp_path, monkeypatch):
        """Harness_env has working_directory; prefetch a file inside it succeeds."""
        # Create a test file in the temp directory
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello():\n    pass\n", encoding="utf-8")

        # Mock get_trace_store to return harness_env with working_directory
        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(
            return_value={"working_directory": str(tmp_path), "workspace_root": None}
        )

        # Mock get_backend to support file caching
        mock_backend = AsyncMock()
        mock_backend.list_cached_files = AsyncMock(return_value=[])
        mock_backend.upsert_file_content = AsyncMock()
        mock_backend.upsert_file_outline = AsyncMock()

        # Mock settings with restriction enabled, no explicit roots
        mock_settings = MagicMock()
        mock_settings.prefetch_allowed_roots = []
        mock_settings.prefetch_restrict_to_workspace = True
        mock_settings.file_cache_max_file_bytes = 500_000

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await prefetch_file("session-1", path=str(test_file))

        # Should succeed and cache the file (not blocked)
        assert "(blocked:" not in result
        assert "Cached:" in result
        assert str(test_file) in result
        mock_backend.upsert_file_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_workspace_set_deny(self, tmp_path, monkeypatch):
        """Harness_env has working_directory; prefetch outside it fails."""
        # Create two directories: workspace and outside
        workspace_dir = tmp_path / "workspace"
        workspace_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()

        test_file = outside_dir / "test.py"
        test_file.write_text("def hello():\n    pass\n", encoding="utf-8")

        # Mock get_trace_store to return harness_env with workspace_dir only
        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(
            return_value={"working_directory": str(workspace_dir), "workspace_root": None}
        )

        # Mock get_backend (should not be called for prefetch outside workspace)
        mock_backend = AsyncMock()
        mock_backend.list_cached_files = AsyncMock(return_value=[])

        # Mock settings with restriction enabled
        mock_settings = MagicMock()
        mock_settings.prefetch_allowed_roots = []
        mock_settings.prefetch_restrict_to_workspace = True
        mock_settings.file_cache_max_file_bytes = 500_000

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await prefetch_file("session-1", path=str(test_file))

        # Should be blocked
        assert "(blocked:" in result
        assert "outside allowed workspace roots" in result

    @pytest.mark.asyncio
    async def test_no_harness_env_deny(self, tmp_path):
        """No harness_env metadata; returns denial message."""
        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello():\n    pass\n", encoding="utf-8")

        # Mock get_trace_store to return None (no harness_env)
        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(return_value=None)

        # Mock get_backend (should not be called)
        mock_backend = AsyncMock()
        mock_backend.list_cached_files = AsyncMock(return_value=[])

        # Mock settings with restriction enabled
        mock_settings = MagicMock()
        mock_settings.prefetch_allowed_roots = []
        mock_settings.prefetch_restrict_to_workspace = True
        mock_settings.file_cache_max_file_bytes = 500_000

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await prefetch_file("session-1", path=str(test_file))

        # Should be blocked with clear denial message
        assert "(blocked: no session workspace on record" in result
        assert "prefetch_allowed_roots" in result
        assert "prefetch_restrict_to_workspace" in result

    @pytest.mark.asyncio
    async def test_explicit_roots_precedence(self, tmp_path):
        """Explicit prefetch_allowed_roots overrides workspace restriction."""
        # Create a file outside the workspace
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir()
        test_file = allowed_dir / "test.py"
        test_file.write_text("def hello():\n    pass\n", encoding="utf-8")

        # Mock get_trace_store (not called because explicit roots take precedence)
        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(return_value=None)

        # Mock get_backend to support file caching
        mock_backend = AsyncMock()
        mock_backend.list_cached_files = AsyncMock(return_value=[])
        mock_backend.upsert_file_content = AsyncMock()
        mock_backend.upsert_file_outline = AsyncMock()

        # Mock settings with explicit roots (harness_env irrelevant)
        mock_settings = MagicMock()
        mock_settings.prefetch_allowed_roots = [str(allowed_dir)]
        mock_settings.prefetch_restrict_to_workspace = True
        mock_settings.file_cache_max_file_bytes = 500_000

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await prefetch_file("session-1", path=str(test_file))

        # Should succeed (explicit roots take precedence, and file is in allowed_dir)
        assert "(blocked:" not in result
        assert "Cached:" in result
        assert str(test_file) in result
        mock_backend.upsert_file_content.assert_called_once()
        # get_session_metadata should NOT be called because explicit roots take precedence
        mock_trace_store.get_session_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_opt_out_unrestricted(self, tmp_path):
        """Restrict flag False, no explicit roots, no harness_env -> unrestricted read succeeds."""
        # Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello():\n    pass\n", encoding="utf-8")

        # Mock get_trace_store (not called because restriction is disabled)
        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(return_value=None)

        # Mock get_backend to support file caching
        mock_backend = AsyncMock()
        mock_backend.list_cached_files = AsyncMock(return_value=[])
        mock_backend.upsert_file_content = AsyncMock()
        mock_backend.upsert_file_outline = AsyncMock()

        # Mock settings with restriction DISABLED
        mock_settings = MagicMock()
        mock_settings.prefetch_allowed_roots = []
        mock_settings.prefetch_restrict_to_workspace = False  # opt-out
        mock_settings.file_cache_max_file_bytes = 500_000

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await prefetch_file("session-1", path=str(test_file))

        # Should succeed (legacy unrestricted behavior)
        assert "(blocked:" not in result
        assert "Cached:" in result
        assert str(test_file) in result
        mock_backend.upsert_file_content.assert_called_once()
        # get_session_metadata should NOT be called because restriction is disabled
        mock_trace_store.get_session_metadata.assert_not_called()
