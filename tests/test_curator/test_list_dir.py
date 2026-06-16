"""Unit tests for the list_dir discovery tool.

Covers:
1. explicit-roots ALLOW: lists entries, subdirectories suffixed with '/'
2. workspace ALLOW: relative path resolves against harness_env working_directory
3. allowlist DENY: absolute path outside the allowed roots is blocked
4. no-workspace DENY: restriction on, no harness_env -> denial message
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ensure_openai_stub() -> None:
    if "openai" not in sys.modules or not hasattr(sys.modules["openai"], "AsyncOpenAI"):
        stub = types.ModuleType("openai")
        stub.AsyncOpenAI = MagicMock()
        stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
        stub.APITimeoutError = type("APITimeoutError", (Exception,), {})
        stub.InternalServerError = type("InternalServerError", (Exception,), {})
        stub.RateLimitError = type("RateLimitError", (Exception,), {})
        sys.modules["openai"] = stub


_ensure_openai_stub()

from archolith_proxy.curator.tools import list_dir  # noqa: E402


def _settings(allowed_roots, restrict=True):
    s = MagicMock()
    s.prefetch_allowed_roots = allowed_roots
    s.prefetch_restrict_to_workspace = restrict
    return s


class TestListDir:
    @pytest.mark.asyncio
    async def test_explicit_roots_lists_entries_with_dir_suffix(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "b.tsx").write_text("export const b = 1;\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()

        mock_settings = _settings([str(tmp_path)])
        with patch("archolith_proxy.config.get_settings", return_value=mock_settings):
            result = await list_dir("session-1", path=str(tmp_path))

        lines = result.splitlines()
        assert "a.py" in lines
        assert "b.tsx" in lines
        assert "sub/" in lines  # subdirectories suffixed with '/'
        assert "(blocked:" not in result

    @pytest.mark.asyncio
    async def test_workspace_relative_path_resolves_against_working_directory(self, tmp_path):
        feature = tmp_path / "features"
        feature.mkdir()
        (feature / "Page.tsx").write_text("// page\n", encoding="utf-8")

        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(
            return_value={"working_directory": str(tmp_path), "workspace_root": None}
        )
        mock_settings = _settings([], restrict=True)

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await list_dir("session-1", path="features")

        assert "Page.tsx" in result
        assert "(blocked:" not in result

    @pytest.mark.asyncio
    async def test_outside_allowed_roots_is_blocked(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        mock_settings = _settings([str(allowed)])
        with patch("archolith_proxy.config.get_settings", return_value=mock_settings):
            result = await list_dir("session-1", path=str(outside))

        assert "(blocked:" in result
        assert "outside allowed workspace roots" in result

    @pytest.mark.asyncio
    async def test_no_workspace_on_record_is_blocked(self, tmp_path):
        mock_trace_store = AsyncMock()
        mock_trace_store.get_session_metadata = AsyncMock(return_value=None)
        mock_settings = _settings([], restrict=True)

        with (
            patch("archolith_proxy.trace.store.get_trace_store", return_value=mock_trace_store),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await list_dir("session-1", path=str(tmp_path))

        assert "(blocked: no session workspace on record" in result
