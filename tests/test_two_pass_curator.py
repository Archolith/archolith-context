"""Tests for two-pass curator — briefing data model, cache, and dispatch logic."""

from __future__ import annotations

import sys
import time
import types
from unittest.mock import MagicMock, patch, AsyncMock

import os

import pytest


# ---------------------------------------------------------------------------
# Settings helper — env-var injection for pydantic-settings priority
# ---------------------------------------------------------------------------

def _set_env_and_reload(**overrides: str) -> None:
    """Set env vars, reset settings cache, so get_settings() picks them up.

    pydantic-settings gives env vars higher priority than .env file values,
    so we use os.environ to force our test overrides.
    """
    from archolith_proxy.config import reset_settings
    reset_settings()
    for key, value in overrides.items():
        os.environ[key] = value


def _clear_env(*keys: str) -> None:
    """Remove test env vars and reset settings cache."""
    from archolith_proxy.config import reset_settings
    reset_settings()
    for key in keys:
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# Bootstrap: inject a stub for the 'openai' top-level package so the
# curator/__init__.py import (`from openai import AsyncOpenAI`) doesn't
# shadow the installed package with our local archolith_proxy/openai/__init__.py.
# ---------------------------------------------------------------------------

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

# Now safe to import curator modules
from archolith_proxy.curator.briefing import (  # noqa: E402
    PreFetchedFile,
    SessionBriefing,
    format_briefing_for_prompt,
    _BRIEFING_MAX_CHARS,
)
from archolith_proxy.curator.result import CuratorResult, CuratorToolCall  # noqa: E402
from archolith_proxy.curator.state import (  # noqa: E402
    cache_briefing,
    get_briefing,
    is_briefing_fresh,
    clear_briefing,
    _briefing_cache,
)

# Force-import _extract_section and _build_briefing_from_result after
# the openai stub is in place (avoids cached-import ordering issues)
import importlib  # noqa: E402
import archolith_proxy.curator  # noqa: E402
importlib.reload(archolith_proxy.curator)

from archolith_proxy.curator import _extract_section, _build_briefing_from_result  # noqa: E402


# ---------------------------------------------------------------------------
# SessionBriefing data model
# ---------------------------------------------------------------------------

class TestSessionBriefing:
    """Test the SessionBriefing dataclass construction and defaults."""

    def test_minimal_construction(self):
        b = SessionBriefing(session_id="s1", source_turn=5)
        assert b.session_id == "s1"
        assert b.source_turn == 5
        assert b.files == []
        assert b.retained_turns is None
        assert b.tool_calls_used == 0
        assert b.context_block == ""

    def test_full_construction(self):
        files = [
            PreFetchedFile(
                path="src/main.py",
                outline="line 1: def main",
                sections=[(1, 10, "def main(): pass")],
                relevance="entry point",
            ),
        ]
        b = SessionBriefing(
            session_id="s1",
            source_turn=3,
            checkpoint_text="State: working",
            open_issues_text="- test failures",
            last_verification_text="pytest: 2 failed",
            decisions_text="- use SQLite",
            session_goal="fix tests",
            facts_text="- uses pytest",
            files=files,
            retained_turns=[1, 3],
            context_block="=== SESSION GOAL ===\nfix tests",
            tool_calls_used=5,
            latency_ms=1200.0,
        )
        assert len(b.files) == 1
        assert b.files[0].path == "src/main.py"
        assert b.retained_turns == [1, 3]
        assert b.checkpoint_text == "State: working"

    def test_timestamp_auto_set(self):
        before = time.time()
        b = SessionBriefing(session_id="s1", source_turn=1)
        after = time.time()
        assert before <= b.timestamp <= after


# ---------------------------------------------------------------------------
# PreFetchedFile
# ---------------------------------------------------------------------------

class TestPreFetchedFile:
    def test_construction(self):
        f = PreFetchedFile(
            path="foo.py",
            outline="line 1: class Foo",
            sections=[(1, 20, "class Foo: pass")],
            relevance="main class",
        )
        assert f.path == "foo.py"
        assert len(f.sections) == 1


# ---------------------------------------------------------------------------
# Briefing cache (state.py)
# ---------------------------------------------------------------------------

class TestBriefingCache:
    def setup_method(self):
        _briefing_cache.clear()

    def test_cache_and_retrieve(self):
        b = SessionBriefing(session_id="s1", source_turn=5)
        cache_briefing("s1", b)
        assert get_briefing("s1") is b

    def test_missing_returns_none(self):
        assert get_briefing("nonexistent") is None

    def test_clear_removes_briefing(self):
        cache_briefing("s1", SessionBriefing(session_id="s1", source_turn=1))
        clear_briefing("s1")
        assert get_briefing("s1") is None

    def test_clear_missing_is_noop(self):
        clear_briefing("never-existed")  # Should not raise

    def test_is_fresh_true_when_source_turn_matches(self):
        b = SessionBriefing(session_id="s1", source_turn=5)
        cache_briefing("s1", b)
        # source_turn=5, current_turn=6 → fresh (5 >= 6-1)
        assert is_briefing_fresh("s1", 6) is True

    def test_is_fresh_true_when_same_turn(self):
        b = SessionBriefing(session_id="s1", source_turn=5)
        cache_briefing("s1", b)
        # source_turn=5, current_turn=5 → fresh (5 >= 5-1)
        assert is_briefing_fresh("s1", 5) is True

    def test_is_fresh_false_when_stale(self):
        b = SessionBriefing(session_id="s1", source_turn=3)
        cache_briefing("s1", b)
        # source_turn=3, current_turn=6 → not fresh (3 < 6-1)
        assert is_briefing_fresh("s1", 6) is False

    def test_is_fresh_false_when_no_briefing(self):
        assert is_briefing_fresh("missing", 5) is False

    def test_overwrite_on_second_cache(self):
        b1 = SessionBriefing(session_id="s1", source_turn=1)
        b2 = SessionBriefing(session_id="s1", source_turn=2)
        cache_briefing("s1", b1)
        cache_briefing("s1", b2)
        assert get_briefing("s1") is b2


# ---------------------------------------------------------------------------
# format_briefing_for_prompt
# ---------------------------------------------------------------------------

class TestFormatBriefingForPrompt:
    def test_empty_briefing(self):
        b = SessionBriefing(session_id="s1", source_turn=1)
        text = format_briefing_for_prompt(b)
        assert "Previous curator context" in text
        assert "turn 1" in text

    def test_with_session_goal(self):
        b = SessionBriefing(session_id="s1", source_turn=2, session_goal="Fix tests")
        text = format_briefing_for_prompt(b)
        assert "=== SESSION GOAL ===" in text
        assert "Fix tests" in text

    def test_with_files(self):
        b = SessionBriefing(
            session_id="s1",
            source_turn=1,
            files=[
                PreFetchedFile(
                    path="main.py",
                    outline="line 1: def main",
                    sections=[(1, 10, "def main(): pass")],
                    relevance="entry point",
                ),
            ],
        )
        text = format_briefing_for_prompt(b)
        assert "=== RELEVANT CODE ===" in text
        assert "main.py lines 1-10" in text

    def test_with_retained_turns(self):
        b = SessionBriefing(
            session_id="s1",
            source_turn=3,
            retained_turns=[1, 3, 5],
        )
        text = format_briefing_for_prompt(b)
        assert "=== RETAINED TURNS ===" in text
        assert "[1, 3, 5]" in text

    def test_with_checkpoint(self):
        b = SessionBriefing(
            session_id="s1",
            source_turn=1,
            checkpoint_text="State: working on auth",
        )
        text = format_briefing_for_prompt(b)
        assert "=== CURRENT STATE ===" in text
        assert "working on auth" in text

    def test_truncation_at_cap(self):
        b = SessionBriefing(
            session_id="s1",
            source_turn=1,
            facts_text="x" * 50_000,
        )
        text = format_briefing_for_prompt(b)
        assert len(text) <= _BRIEFING_MAX_CHARS + 500  # Allow for wrapping text

    def test_freshness_guidance(self):
        b = SessionBriefing(session_id="s1", source_turn=7)
        text = format_briefing_for_prompt(b)
        assert "turn 7" in text
        assert "emit it directly" in text


# ---------------------------------------------------------------------------
# _build_briefing_from_result (in curator/__init__.py)
# ---------------------------------------------------------------------------

class TestBuildBriefingFromResult:
    def test_basic_result_to_briefing(self):
        result = CuratorResult(
            context_text=(
                "=== SESSION GOAL ===\nFix tests\n\n"
                "=== CURRENT STATE ===\nWorking\n\n"
                "=== KEY FACTS ===\n- Uses pytest\n\n"
                "=== DECISIONS ===\n- Use SQLite\n"
            ),
            curated_paths={"src/main.py"},
            tool_calls_used=3,
            estimated_tokens=100,
            retained_turn_numbers=[1, 3],
            tool_log=[
                CuratorToolCall(
                    tool="get_file",
                    args={"path": "src/main.py"},
                    status="ok",
                    result_preview="def main(): pass",
                ),
            ],
        )

        briefing = _build_briefing_from_result(
            result=result,
            session_id="s1",
            turn_number=5,
            latency_ms=1500.0,
            session_goal="Fix tests",
            messages=[],
        )

        assert briefing.session_id == "s1"
        assert briefing.source_turn == 5
        assert briefing.session_goal == "Fix tests"
        assert briefing.retained_turns == [1, 3]
        assert briefing.latency_ms == 1500.0
        assert "Uses pytest" in briefing.facts_text
        assert "Working" in briefing.checkpoint_text
        assert len(briefing.files) == 1
        assert briefing.files[0].path == "src/main.py"

    def test_empty_result_sections(self):
        result = CuratorResult(
            context_text="=== SESSION GOAL ===\nHello\n",
            curated_paths=set(),
            tool_calls_used=0,
            estimated_tokens=10,
        )

        briefing = _build_briefing_from_result(
            result=result,
            session_id="s1",
            turn_number=1,
            latency_ms=200.0,
            session_goal=None,
            messages=[],
        )

        assert briefing.checkpoint_text == ""
        assert briefing.facts_text == ""
        assert briefing.files == []


# ---------------------------------------------------------------------------
# _extract_section helper
# ---------------------------------------------------------------------------

class TestExtractSection:
    def test_extracts_middle_section(self):
        text = (
            "=== SESSION GOAL ===\nFix tests\n\n"
            "=== CURRENT STATE ===\nWorking on auth\n\n"
            "=== KEY FACTS ===\n- Uses pytest\n"
        )
        assert _extract_section(text, "CURRENT STATE") == "Working on auth"
        assert _extract_section(text, "KEY FACTS") == "- Uses pytest"

    def test_missing_section_returns_empty(self):
        text = "=== SESSION GOAL ===\nFix tests\n"
        assert _extract_section(text, "CURRENT STATE") == ""

    def test_last_section_without_trailing_delimiter(self):
        text = "=== SESSION GOAL ===\nFix tests\n=== CURRENT STATE ===\nWorking\n"
        assert _extract_section(text, "CURRENT STATE") == "Working"


# ---------------------------------------------------------------------------
# Integration: curate_context dispatch with briefing
# ---------------------------------------------------------------------------

class TestCurateContextDispatch:
    """Test that curate_context dispatches correctly between briefing and full curator.

    These tests mock _run_curator_native to avoid real LLM calls.
    """

    def setup_method(self):
        _briefing_cache.clear()
        # Reset settings between tests
        from archolith_proxy.config import reset_settings
        reset_settings()

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self):
        from archolith_proxy.curator import curate_context
        from archolith_proxy.config import get_settings

        # Default: curator_enabled=False
        result = await curate_context(
            session_id="s1", turn_number=5,
            user_message="test", session_goal=None,
            http_client=None, messages=[{"role": "user", "content": "hi"}] * 6,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cold_start_returns_none(self):
        from archolith_proxy.curator import curate_context
        from archolith_proxy.config import Settings, reset_settings, _settings
        import archolith_proxy.config as cfg

        # Set curator enabled but not enough turns
        cfg._settings = Settings(
            CURATOR_ENABLED=True,
            FILE_CACHE_ENABLED=True,
            EXTRACTOR_API_KEY="test-key",
            UPSTREAM_API_KEY="test-key",
        )
        result = await curate_context(
            session_id="s1", turn_number=1,
            user_message="test", session_goal=None,
            http_client=None, messages=[{"role": "user", "content": "hi"}],
        )
        assert result is None
        cfg._settings = None  # cleanup


# ---------------------------------------------------------------------------
# Integration: run_background_pass with mocked _run_curator_native
# ---------------------------------------------------------------------------

class TestBackgroundPassPipeline:
    """Test run_background_pass end-to-end with mocked curator loop."""

    _ENV_KEYS = (
        "CURATOR_ENABLED", "FILE_CACHE_ENABLED", "BACKGROUND_PASS_ENABLED",
        "BACKGROUND_PASS_DEBOUNCE_MS", "BACKGROUND_PASS_LATENCY_BUDGET_MS",
        "EXTRACTOR_API_KEY", "CURATOR_API_KEY",
    )

    def setup_method(self):
        _briefing_cache.clear()
        from archolith_proxy.config import reset_settings
        reset_settings()

    def teardown_method(self):
        _clear_env(*self._ENV_KEYS)

    @pytest.mark.asyncio
    async def test_background_pass_caches_briefing(self):
        """Background pass runs curator, builds briefing, and caches it."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            BACKGROUND_PASS_ENABLED="true",
            BACKGROUND_PASS_DEBOUNCE_MS="0",
            BACKGROUND_PASS_LATENCY_BUDGET_MS="30000",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass
        from archolith_proxy.config import get_settings

        mock_result = CuratorResult(
            context_text=(
                "=== SESSION GOAL ===\nFix auth\n\n"
                "=== CURRENT STATE ===\nWorking\n\n"
                "=== KEY FACTS ===\n- Uses JWT\n"
            ),
            curated_paths={"auth/handler.py"},
            tool_calls_used=2,
            estimated_tokens=50,
            retained_turn_numbers=[1, 2],
            tool_log=[
                CuratorToolCall(
                    tool="get_file",
                    args={"path": "auth/handler.py"},
                    status="ok",
                    result_preview="def handle_auth(): pass",
                    raw_result="def handle_auth(): pass\n    token = jwt.encode({})\n    return token\n",
                ),
            ],
        )

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (mock_result, mock_result.tool_log, "")
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                await run_background_pass(
                    session_id="s1",
                    turn_number=5,
                    user_message="fix auth",
                    session_goal="Fix auth",
                    messages=[{"role": "user", "content": "fix auth"}],
                )

        briefing = get_briefing("s1")
        assert briefing is not None
        assert briefing.source_turn == 5
        assert briefing.session_goal == "Fix auth"
        assert len(briefing.files) == 1
        assert briefing.files[0].path == "auth/handler.py"
        # Verify full content is captured (not truncated preview)
        assert "jwt.encode" in briefing.files[0].sections[0][2]
        assert briefing.retained_turns == [1, 2]

    @pytest.mark.asyncio
    async def test_background_pass_disabled_skips(self):
        """When background_pass_enabled=False, run_background_pass does nothing."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            BACKGROUND_PASS_ENABLED="false",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass

        await run_background_pass(
            session_id="s1", turn_number=5,
            user_message="test", session_goal=None, messages=[],
        )
        assert get_briefing("s1") is None

    @pytest.mark.asyncio
    async def test_background_pass_timeout_returns_silently(self):
        """Background pass timeout logs and returns without caching."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            BACKGROUND_PASS_ENABLED="true",
            BACKGROUND_PASS_DEBOUNCE_MS="0",
            BACKGROUND_PASS_LATENCY_BUDGET_MS="100",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass
        import asyncio

        async def _slow_curator(**kwargs):
            await asyncio.sleep(5)  # exceeds 100ms budget
            return (None, [], "")

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock, side_effect=_slow_curator):
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                await run_background_pass(
                    session_id="s1", turn_number=5,
                    user_message="test", session_goal=None, messages=[],
                )

        # No briefing cached after timeout
        assert get_briefing("s1") is None

    @pytest.mark.asyncio
    async def test_background_pass_curator_returns_none(self):
        """When curator loop returns None result, no briefing is cached."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            BACKGROUND_PASS_ENABLED="true",
            BACKGROUND_PASS_DEBOUNCE_MS="0",
            BACKGROUND_PASS_LATENCY_BUDGET_MS="30000",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (None, [], "empty_response")
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                await run_background_pass(
                    session_id="s1", turn_number=5,
                    user_message="test", session_goal=None, messages=[],
                )

        assert get_briefing("s1") is None


# ---------------------------------------------------------------------------
# Integration: inline pass with briefing (full pipeline)
# ---------------------------------------------------------------------------

class TestInlineBriefingPipeline:
    """Test the inline briefing pass with mocked _run_curator_native."""

    _ENV_KEYS = (
        "CURATOR_ENABLED", "FILE_CACHE_ENABLED", "CURATOR_API_KEY",
        "CURATOR_LATENCY_BUDGET_MS", "CURATOR_MAX_ITERATIONS",
        "COHERENCE_TAIL_SIZE", "MAX_TAIL_MESSAGES", "COLD_START_TURNS",
    )

    def setup_method(self):
        _briefing_cache.clear()
        from archolith_proxy.config import reset_settings
        reset_settings()

    def teardown_method(self):
        _clear_env(*self._ENV_KEYS)

    @pytest.mark.asyncio
    async def test_inline_pass_with_fresh_briefing(self):
        """Inline pass reads briefing, runs curator with 2 iterations, returns context."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            CURATOR_API_KEY="test-key",
            CURATOR_LATENCY_BUDGET_MS="10000",
            COHERENCE_TAIL_SIZE="10",
            MAX_TAIL_MESSAGES="20",
            COLD_START_TURNS="3",
        )
        from archolith_proxy.curator import curate_context

        # Cache a fresh briefing for session s1
        briefing = SessionBriefing(
            session_id="s1",
            source_turn=5,
            session_goal="Fix auth",
            checkpoint_text="Working on JWT",
            files=[
                PreFetchedFile(
                    path="auth.py",
                    outline="",
                    sections=[(1, 10, "def handle_auth(): pass")],
                    relevance="entry point",
                ),
            ],
            context_block="=== SESSION GOAL ===\nFix auth\n",
        )
        cache_briefing("s1", briefing)

        # Mock the inline pass curator to return a context
        inline_result = CuratorResult(
            context_text="=== SESSION GOAL ===\nFix auth\n=== CURRENT STATE ===\nJWT working\n",
            curated_paths={"auth.py"},
            tool_calls_used=0,
            estimated_tokens=40,
            retained_turn_numbers=[1, 2],
            tool_log=[],
        )

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (inline_result, [], "")
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                result = await curate_context(
                    session_id="s1",
                    turn_number=6,  # source_turn=5, turn=6 → fresh
                    user_message="continue fixing auth",
                    session_goal="Fix auth",
                    http_client=None,
                    messages=[{"role": "user", "content": "hi"}] * 5,
                )

        assert result is not None
        assert "JWT working" in result.system_message["content"]
        assert result.session_id == "s1"
        assert result.retained_turn_numbers == [1, 2]
        # Verify the inline pass was called with 2 iterations (not full 6)
        mock_loop.assert_called_once()
        call_kwargs = mock_loop.call_args.kwargs
        assert call_kwargs.get("max_iterations") == 2

    @pytest.mark.asyncio
    async def test_fallback_to_full_curator_on_briefing_failure(self):
        """When inline briefing pass returns None, falls back to full curator."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            CURATOR_API_KEY="test-key",
            CURATOR_MAX_ITERATIONS="6",
            CURATOR_LATENCY_BUDGET_MS="10000",
            COHERENCE_TAIL_SIZE="10",
            MAX_TAIL_MESSAGES="20",
            COLD_START_TURNS="3",
        )
        from archolith_proxy.curator import curate_context

        # Cache a stale briefing (source_turn=4, turn=6 → stale but within threshold 4 >= 6-2)
        briefing = SessionBriefing(
            session_id="s1",
            source_turn=4,
            session_goal="Fix auth",
        )
        cache_briefing("s1", briefing)

        full_result = CuratorResult(
            context_text="=== SESSION GOAL ===\nFix auth (full pass)\n",
            curated_paths=set(),
            tool_calls_used=4,
            estimated_tokens=60,
            tool_log=[],
        )

        call_count = 0

        async def _mock_curator(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: inline briefing pass → returns None
                return (None, [], "empty_final")
            else:
                # Second call: full curator fallback
                return (full_result, [], "")

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock, side_effect=_mock_curator):
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                result = await curate_context(
                    session_id="s1",
                    turn_number=6,
                    user_message="fix auth",
                    session_goal="Fix auth",
                    http_client=None,
                    messages=[{"role": "user", "content": "hi"}] * 5,
                )

        assert result is not None
        assert "full pass" in result.system_message["content"]
        # Two calls: first for inline briefing, second for full curator
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_briefing_runs_full_curator(self):
        """When no briefing exists, runs the standard full curator path."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            CURATOR_API_KEY="test-key",
            CURATOR_MAX_ITERATIONS="6",
            CURATOR_LATENCY_BUDGET_MS="10000",
            COHERENCE_TAIL_SIZE="10",
            MAX_TAIL_MESSAGES="20",
            COLD_START_TURNS="3",
        )
        from archolith_proxy.curator import curate_context

        full_result = CuratorResult(
            context_text="=== SESSION GOAL ===\nStandard pass\n",
            curated_paths={"app.py"},
            tool_calls_used=3,
            estimated_tokens=50,
            tool_log=[],
        )

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (full_result, [], "")
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                result = await curate_context(
                    session_id="s1",
                    turn_number=6,
                    user_message="hello",
                    session_goal=None,
                    http_client=None,
                    messages=[{"role": "user", "content": "hi"}] * 5,
                )

        assert result is not None
        assert "Standard pass" in result.system_message["content"]
        # Only one call — the full curator (no briefing path attempted)
        mock_loop.assert_called_once()
        call_kwargs = mock_loop.call_args.kwargs
        assert call_kwargs.get("max_iterations") == 6


# ---------------------------------------------------------------------------
# raw_result fidelity test
# ---------------------------------------------------------------------------

class TestRawResultFidelity:
    """Verify that briefings capture full tool result content, not truncated previews."""

    def test_briefing_uses_raw_result_not_preview(self):
        """_build_briefing_from_result should capture raw_result, not 200-char preview."""
        long_content = "x" * 5000  # much longer than 200-char preview
        result = CuratorResult(
            context_text="=== SESSION GOAL ===\nTest\n",
            curated_paths={"big_file.py"},
            tool_calls_used=1,
            estimated_tokens=100,
            tool_log=[
                CuratorToolCall(
                    tool="get_file",
                    args={"path": "big_file.py", "start_line": 1, "end_line": 100},
                    status="ok",
                    result_preview=long_content[:200],
                    raw_result=long_content,
                ),
            ],
        )

        briefing = _build_briefing_from_result(
            result=result,
            session_id="s1",
            turn_number=1,
            latency_ms=100.0,
            session_goal=None,
            messages=[],
        )

        assert len(briefing.files) == 1
        # The section content should be the full raw_result, not the preview
        section_content = briefing.files[0].sections[0][2]
        assert len(section_content) == 5000
        assert section_content == long_content

    def test_briefing_falls_back_to_preview_when_no_raw(self):
        """If raw_result is empty, fall back to result_preview."""
        result = CuratorResult(
            context_text="=== SESSION GOAL ===\nTest\n",
            curated_paths={"old.py"},
            tool_calls_used=1,
            estimated_tokens=50,
            tool_log=[
                CuratorToolCall(
                    tool="get_file",
                    args={"path": "old.py"},
                    status="ok",
                    result_preview="short preview",
                    raw_result="",  # empty raw — backward compat
                ),
            ],
        )

        briefing = _build_briefing_from_result(
            result=result,
            session_id="s1",
            turn_number=1,
            latency_ms=50.0,
            session_goal=None,
            messages=[],
        )

        assert len(briefing.files) == 1
        assert briefing.files[0].sections[0][2] == "short preview"


# ---------------------------------------------------------------------------
# Registration hooks — register_curation_mode / unregister_curation_mode
# ---------------------------------------------------------------------------

import archolith_proxy.curator as _curator_module  # noqa: E402
importlib.reload(_curator_module)

from archolith_proxy.curator import (  # noqa: E402
    register_curation_mode,
    unregister_curation_mode,
)
from archolith_proxy.models.dtos import AssembledContext  # noqa: E402


class TestRegistrationHooks:
    """Test curation mode registration and unregistration."""

    def setup_method(self):
        unregister_curation_mode()

    def teardown_method(self):
        unregister_curation_mode()

    def test_register_background_pass_only(self):
        async def my_bg_fn(*args, **kwargs):
            return None

        register_curation_mode(background_pass_fn=my_bg_fn)
        assert _curator_module._background_pass_fn is my_bg_fn
        assert _curator_module._inline_pass_fn is None

    def test_register_inline_pass_only(self):
        async def my_inline_fn(*args, **kwargs):
            return None

        register_curation_mode(inline_pass_fn=my_inline_fn)
        assert _curator_module._inline_pass_fn is my_inline_fn
        assert _curator_module._background_pass_fn is None

    def test_register_both(self):
        async def my_bg_fn(*args, **kwargs):
            return None

        async def my_inline_fn(*args, **kwargs):
            return None

        register_curation_mode(background_pass_fn=my_bg_fn, inline_pass_fn=my_inline_fn)
        assert _curator_module._background_pass_fn is my_bg_fn
        assert _curator_module._inline_pass_fn is my_inline_fn

    def test_register_none_leaves_unregistered(self):
        register_curation_mode()  # both None — no change
        assert _curator_module._background_pass_fn is None
        assert _curator_module._inline_pass_fn is None

    def test_register_does_not_unregister_existing(self):
        async def bg1(*args, **kwargs):
            return None

        async def bg2(*args, **kwargs):
            return None

        register_curation_mode(background_pass_fn=bg1)
        register_curation_mode(inline_pass_fn=bg2)  # only sets inline
        assert _curator_module._background_pass_fn is bg1  # bg unchanged
        assert _curator_module._inline_pass_fn is bg2

    def test_unregister_clears_both(self):
        async def my_fn(*args, **kwargs):
            return None

        register_curation_mode(background_pass_fn=my_fn, inline_pass_fn=my_fn)
        unregister_curation_mode()
        assert _curator_module._background_pass_fn is None
        assert _curator_module._inline_pass_fn is None


# ---------------------------------------------------------------------------
# Integration: background pass dispatch with registered mode function
# ---------------------------------------------------------------------------

class TestBackgroundPassDispatch:
    """Test that run_background_pass dispatches to registered function."""

    _ENV_KEYS = (
        "CURATOR_ENABLED", "FILE_CACHE_ENABLED", "BACKGROUND_PASS_ENABLED",
        "BACKGROUND_PASS_DEBOUNCE_MS", "EXTRACTOR_API_KEY",
    )

    def setup_method(self):
        _briefing_cache.clear()
        unregister_curation_mode()
        from archolith_proxy.config import reset_settings
        reset_settings()

    def teardown_method(self):
        _clear_env(*self._ENV_KEYS)
        unregister_curation_mode()

    @pytest.mark.asyncio
    async def test_registered_bg_fn_is_called(self):
        """When a background pass fn is registered, run_background_pass calls it."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            BACKGROUND_PASS_ENABLED="true",
            BACKGROUND_PASS_DEBOUNCE_MS="0",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass

        called = False

        async def my_bg_fn(session_id, turn_number, user_message, session_goal, messages):
            nonlocal called
            called = True
            return None

        register_curation_mode(background_pass_fn=my_bg_fn)

        await run_background_pass(
            session_id="s1", turn_number=5,
            user_message="test", session_goal=None, messages=[],
        )
        assert called, "Registered background pass fn was not called"

    @pytest.mark.asyncio
    async def test_registered_bg_fn_caches_briefing(self):
        """When registered bg fn returns a briefing, it is cached."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            BACKGROUND_PASS_ENABLED="true",
            BACKGROUND_PASS_DEBOUNCE_MS="0",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass

        expected_briefing = SessionBriefing(session_id="s1", source_turn=5, session_goal="test")

        async def my_bg_fn(session_id, turn_number, user_message, session_goal, messages):
            return expected_briefing

        register_curation_mode(background_pass_fn=my_bg_fn)

        await run_background_pass(
            session_id="s1", turn_number=5,
            user_message="test", session_goal="test", messages=[],
        )
        cached = get_briefing("s1")
        assert cached is expected_briefing

    @pytest.mark.asyncio
    async def test_default_bg_fn_used_when_no_registration(self):
        """Without registration, run_background_pass uses the default _run_background_pass_inner."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            BACKGROUND_PASS_ENABLED="true",
            BACKGROUND_PASS_DEBOUNCE_MS="0",
            BACKGROUND_PASS_LATENCY_BUDGET_MS="30000",
            EXTRACTOR_API_KEY="test-key",
        )
        from archolith_proxy.curator import run_background_pass

        mock_result = CuratorResult(
            context_text="=== SESSION GOAL ===\nTest\n",
            curated_paths={"test.py"},
            tool_calls_used=1,
            estimated_tokens=10,
            tool_log=[
                CuratorToolCall(
                    tool="get_file",
                    args={"path": "test.py"},
                    status="ok",
                    result_preview="test content",
                    raw_result="test content",
                ),
            ],
        )

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (mock_result, mock_result.tool_log, "")
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                await run_background_pass(
                    session_id="s1", turn_number=5,
                    user_message="test", session_goal="Test", messages=[],
                )

        # Default path should have run via _run_background_pass_inner,
        # which calls _run_curator_native
        mock_loop.assert_called_once()
        briefing = get_briefing("s1")
        assert briefing is not None
        assert briefing.session_goal == "Test"


# ---------------------------------------------------------------------------
# Integration: inline pass dispatch with registered mode function
# ---------------------------------------------------------------------------

class TestInlinePassDispatch:
    """Test that curate_context dispatches to registered inline pass."""

    _ENV_KEYS = (
        "CURATOR_ENABLED", "FILE_CACHE_ENABLED", "CURATOR_API_KEY",
        "CURATOR_LATENCY_BUDGET_MS", "COHERENCE_TAIL_SIZE",
        "MAX_TAIL_MESSAGES", "COLD_START_TURNS",
    )

    def setup_method(self):
        _briefing_cache.clear()
        unregister_curation_mode()
        from archolith_proxy.config import reset_settings
        reset_settings()

    def teardown_method(self):
        _clear_env(*self._ENV_KEYS)
        unregister_curation_mode()

    @pytest.mark.asyncio
    async def test_registered_inline_fn_is_called(self):
        """When an inline pass fn is registered, curate_context calls it for briefing passes."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            CURATOR_API_KEY="test-key",
            CURATOR_LATENCY_BUDGET_MS="10000",
            COHERENCE_TAIL_SIZE="10",
            MAX_TAIL_MESSAGES="20",
            COLD_START_TURNS="3",
        )
        from archolith_proxy.curator import curate_context

        # Cache a fresh briefing
        briefing = SessionBriefing(session_id="s1", source_turn=5, session_goal="test")
        cache_briefing("s1", briefing)

        called = False

        async def my_inline_fn(session_id, turn_number, user_message, session_goal,
                               briefing, messages, client, model, settings):
            nonlocal called
            called = True
            return None

        register_curation_mode(inline_pass_fn=my_inline_fn)

        # Mock the full curator path so the fallback doesn't actually run
        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            result = await curate_context(
                session_id="s1", turn_number=6,
                user_message="test", session_goal="test",
                http_client=None,
                messages=[{"role": "user", "content": "hi"}] * 5,
            )

        assert called, "Registered inline pass fn was not called"
        # The registered inline fn returned None, so it should fall through
        # to the full curator path. result is None because full curator is mocked.
        # The important thing is that my_inline_fn was called.
        assert result is None

    @pytest.mark.asyncio
    async def test_registered_inline_fn_returns_context(self):
        """When registered inline fn returns an AssembledContext, it is used directly."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            CURATOR_API_KEY="test-key",
            CURATOR_LATENCY_BUDGET_MS="10000",
            COHERENCE_TAIL_SIZE="10",
            MAX_TAIL_MESSAGES="20",
            COLD_START_TURNS="3",
        )
        from archolith_proxy.curator import curate_context

        briefing = SessionBriefing(session_id="s1", source_turn=5)
        cache_briefing("s1", briefing)

        expected_result = AssembledContext(
            system_message={"role": "system", "content": "from registered inline"},
            graph_context=[{"role": "system", "content": "from registered inline"}],
            coherence_tail=[],
            token_estimate=50,
            session_id="s1",
        )

        async def my_inline_fn(session_id, turn_number, user_message, session_goal,
                               briefing, messages, client, model, settings):
            return expected_result

        register_curation_mode(inline_pass_fn=my_inline_fn)

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            result = await curate_context(
                session_id="s1", turn_number=6,
                user_message="test", session_goal="test",
                http_client=None,
                messages=[{"role": "user", "content": "hi"}] * 5,
            )

        assert result is expected_result
        # Verify the full curator was never called
        mock_loop.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_inline_used_when_no_registration(self):
        """Without registration, curate_context uses _run_with_briefing."""
        _set_env_and_reload(
            CURATOR_ENABLED="true",
            FILE_CACHE_ENABLED="true",
            CURATOR_API_KEY="test-key",
            CURATOR_LATENCY_BUDGET_MS="10000",
            COHERENCE_TAIL_SIZE="10",
            MAX_TAIL_MESSAGES="20",
            COLD_START_TURNS="3",
        )
        from archolith_proxy.curator import curate_context

        briefing = SessionBriefing(
            session_id="s1",
            source_turn=5,
            session_goal="test",
            checkpoint_text="State: ok",
        )
        cache_briefing("s1", briefing)

        inline_result = CuratorResult(
            context_text="=== SESSION GOAL ===\ntest\n=== CURRENT STATE ===\nok\n",
            curated_paths=set(),
            tool_calls_used=0,
            estimated_tokens=20,
            tool_log=[],
        )

        with patch("archolith_proxy.curator._run_curator_native",
                    new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = (inline_result, [], "")
            with patch("archolith_proxy.graph.backend.is_graph_ready", return_value=False):
                result = await curate_context(
                    session_id="s1", turn_number=6,
                    user_message="test", session_goal="test",
                    http_client=None,
                    messages=[{"role": "user", "content": "hi"}] * 5,
                )

        # Default _run_with_briefing was called → executed _run_curator_native with 2 iters
        assert result is not None
        mock_loop.assert_called_once()
        assert mock_loop.call_args.kwargs.get("max_iterations") == 2


# ---------------------------------------------------------------------------
# SessionBriefing.mode field
# ---------------------------------------------------------------------------

class TestBriefingModeField:
    """Test the new mode field on SessionBriefing."""

    def test_mode_defaults_to_two_pass(self):
        b = SessionBriefing(session_id="s1", source_turn=1)
        assert b.mode == "two_pass"

    def test_mode_can_be_set(self):
        b = SessionBriefing(session_id="s1", source_turn=1, mode="two_curator")
        assert b.mode == "two_curator"

    def test_build_briefing_sets_mode(self):
        """_build_briefing_from_result should set mode='two_pass'."""
        result = CuratorResult(
            context_text="=== SESSION GOAL ===\nTest\n",
            curated_paths=set(),
            tool_calls_used=0,
            estimated_tokens=10,
        )
        briefing = _build_briefing_from_result(
            result=result,
            session_id="s1",
            turn_number=5,
            latency_ms=100.0,
            session_goal="Test",
            messages=[],
        )
        assert briefing.mode == "two_pass"


# ---------------------------------------------------------------------------
# CuratorResult.assembly_mode field
# ---------------------------------------------------------------------------

class TestCuratorResultAssemblyMode:
    """Test the new assembly_mode field on CuratorResult."""

    def test_defaults_to_curator(self):
        r = CuratorResult(context_text="test")
        assert r.assembly_mode == "curator"

    def test_can_be_set(self):
        r = CuratorResult(context_text="test", assembly_mode="briefing")
        assert r.assembly_mode == "briefing"

    def test_can_be_set_stale(self):
        r = CuratorResult(context_text="test", assembly_mode="briefing_stale")
        assert r.assembly_mode == "briefing_stale"
