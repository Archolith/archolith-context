"""Tests for two-pass curator — briefing data model, cache, and dispatch logic."""

from __future__ import annotations

import sys
import time
import types
from unittest.mock import MagicMock

import pytest


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
