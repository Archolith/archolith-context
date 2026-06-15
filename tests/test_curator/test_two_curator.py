"""Tests for the two-curator mode (prepper + assembler).

Covers:
- configure_curation_mode() with curation_mode="two_curator"
- configure_curation_mode() with curation_mode="two_pass" (backward compat)
- register_curation_mode() / unregister_curation_mode()
- Prepper tool set (PREPPER_TOOLS includes score_file_relevance)
- Assembler tool set (ASSEMBLER_TOOLS is minimal)
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

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
from archolith_proxy.curator import (  # noqa: E402
    register_curation_mode,
    unregister_curation_mode,
)

# Access module globals via the module to avoid import-time copy issues
import archolith_proxy.curator as _curator_mod  # noqa: E402

def _get_bg_fn():
    return _curator_mod._background_pass_fn

def _get_inline_fn():
    return _curator_mod._inline_pass_fn


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_register_curation_mode_sets_both():
    """register_curation_mode() with both functions sets both globals."""
    bp = MagicMock()
    ip = MagicMock()
    register_curation_mode(background_pass_fn=bp, inline_pass_fn=ip)
    assert _get_bg_fn() is bp
    assert _get_inline_fn() is ip


def test_register_curation_mode_partial():
    """register_curation_mode() with only one function leaves the other unchanged."""
    bp = MagicMock()
    ip = MagicMock()

    # Set both first
    register_curation_mode(background_pass_fn=bp, inline_pass_fn=ip)
    assert _get_bg_fn() is bp
    assert _get_inline_fn() is ip

    # Now set only background
    bp2 = MagicMock()
    register_curation_mode(background_pass_fn=bp2)
    assert _get_bg_fn() is bp2
    assert _get_inline_fn() is ip  # unchanged

    # Now set only inline
    ip2 = MagicMock()
    register_curation_mode(inline_pass_fn=ip2)
    assert _get_bg_fn() is bp2  # unchanged
    assert _get_inline_fn() is ip2


def test_unregister_curation_mode_clears_both():
    """unregister_curation_mode() clears both globals."""
    bp = MagicMock()
    ip = MagicMock()
    register_curation_mode(background_pass_fn=bp, inline_pass_fn=ip)
    unregister_curation_mode()
    assert _get_bg_fn() is None
    assert _get_inline_fn() is None


# ---------------------------------------------------------------------------
# configure_curation_mode tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registration():
    """Ensure clean registration state before each test."""
    unregister_curation_mode()
    yield
    unregister_curation_mode()


@patch("archolith_proxy.curator.get_settings")
def test_configure_two_curator_registers(mock_settings):
    """configure_curation_mode() with curation_mode='two_curator' registers both functions."""
    settings = MagicMock()
    settings.curation_mode = "two_curator"
    settings.prepper_model = ""
    settings.curator_model = "curie"
    settings.extractor_model = "extractor"
    settings.assembler_model = ""
    settings.assembler_deterministic = False  # default: register the LLM assembler
    mock_settings.return_value = settings

    from archolith_proxy.curator import configure_curation_mode
    configure_curation_mode()

    assert _get_bg_fn() is not None
    assert _get_inline_fn() is not None
    assert _get_bg_fn().__name__ == "run_prepper"
    assert _get_inline_fn().__name__ == "run_assembler"


@patch("archolith_proxy.curator.get_settings")
def test_configure_two_curator_deterministic_registers_deterministic_assembler(mock_settings):
    """assembler_deterministic=True registers the LLM-free deterministic assembler."""
    settings = MagicMock()
    settings.curation_mode = "two_curator"
    settings.prepper_model = ""
    settings.curator_model = "curie"
    settings.extractor_model = "extractor"
    settings.assembler_model = ""
    settings.assembler_deterministic = True
    mock_settings.return_value = settings

    from archolith_proxy.curator import configure_curation_mode
    configure_curation_mode()

    assert _get_bg_fn().__name__ == "run_prepper"
    assert _get_inline_fn().__name__ == "run_deterministic_assembler"


@patch("archolith_proxy.curator.get_settings")
def test_configure_two_pass_unregisters(mock_settings):
    """configure_curation_mode() with curation_mode='two_pass' clears registration."""
    # Register something first
    register_curation_mode(background_pass_fn=MagicMock(), inline_pass_fn=MagicMock())
    assert _get_bg_fn() is not None

    settings = MagicMock()
    settings.curation_mode = "two_pass"
    mock_settings.return_value = settings

    from archolith_proxy.curator import configure_curation_mode
    configure_curation_mode()

    assert _get_bg_fn() is None
    assert _get_inline_fn() is None


# ---------------------------------------------------------------------------
# Prepper tool set tests
# ---------------------------------------------------------------------------


def test_prepper_tools_include_score_relevance():
    """PREPPER_TOOLS includes score_file_relevance and all curator tools."""
    from archolith_proxy.curator.schemas import PREPPER_TOOLS, ALL_CURATOR_TOOLS

    prepper_names = {t["function"]["name"] for t in PREPPER_TOOLS}
    curator_names = {t["function"]["name"] for t in ALL_CURATOR_TOOLS}

    # Prepper has all curator tools
    for name in curator_names:
        assert name in prepper_names, f"PREPPER_TOOLS missing {name}"

    # Prepper has score_file_relevance
    assert "score_file_relevance" in prepper_names


def test_prepper_tools_superset():
    """PREPPER_TOOLS is a proper superset of ALL_CURATOR_TOOLS."""
    from archolith_proxy.curator.schemas import PREPPER_TOOLS, ALL_CURATOR_TOOLS

    assert len(PREPPER_TOOLS) == len(ALL_CURATOR_TOOLS) + 1


# ---------------------------------------------------------------------------
# Assembler tool set tests
# ---------------------------------------------------------------------------


def test_assembler_tools_minimal():
    """ASSEMBLER_TOOLS contains only select_relevant_turns and get_file_lines."""
    from archolith_proxy.curator.schemas import ASSEMBLER_TOOLS

    names = {t["function"]["name"] for t in ASSEMBLER_TOOLS}
    assert names == {"select_relevant_turns", "get_file_lines"}


def test_assembler_tools_no_broad_tools():
    """ASSEMBLER_TOOLS does NOT contain broad tools like search_facts, get_checkpoint."""
    from archolith_proxy.curator.schemas import ASSEMBLER_TOOLS

    names = {t["function"]["name"] for t in ASSEMBLER_TOOLS}
    broad_tools = {"search_facts", "search_facts_semantic", "get_checkpoint",
                    "get_open_issues", "get_last_verification", "prefetch_file",
                    "get_file_outline", "score_file_relevance"}
    for tool in broad_tools:
        assert tool not in names, f"ASSEMBLER_TOOLS should not contain {tool}"


# ---------------------------------------------------------------------------
# score_file_relevance tool handler tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_file_relevance_empty_query():
    """score_file_relevance with empty query returns guidance message."""
    from archolith_proxy.curator.tools import score_file_relevance
    result = await score_file_relevance(session_id="test", query="")
    assert "no query specified" in result


@pytest.mark.asyncio
async def test_score_file_relevance_no_files():
    """score_file_relevance with no cached files returns appropriate message."""
    from archolith_proxy.curator.tools import score_file_relevance

    async def _empty_list(*args, **kwargs):
        return []

    with patch("archolith_proxy.curator.tools.get_backend") as mock_backend:
        mock_backend.return_value.list_cached_files = _empty_list
        result = await score_file_relevance(session_id="test", query="auth handler")
        assert "no cached files" in result


@pytest.mark.asyncio
async def test_score_file_relevance_ranks_files():
    """score_file_relevance ranks files by keyword match."""
    from archolith_proxy.curator.tools import score_file_relevance
    files = [
        {"path": "/src/auth/handler.py", "outline": "login() logout()", "last_updated_turn": 5},
        {"path": "/src/db/models.py", "outline": "User Model", "last_updated_turn": 2},
        {"path": "/src/config/settings.py", "outline": "API_KEY", "last_updated_turn": 1},
    ]

    async def _get_files(*args, **kwargs):
        return files

    with patch("archolith_proxy.curator.tools.get_backend") as mock_backend:
        mock_backend.return_value.list_cached_files = _get_files
        result = await score_file_relevance(session_id="test", query="auth login handler")
        # auth/handler.py should be ranked first (keyword match + recency)
        assert "handler.py" in result
        assert "auth" in result or "login" in result


# ---------------------------------------------------------------------------
# Prepper module tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_prepper_no_api_key():
    """run_prepper() returns None when no API key is configured."""
    from archolith_proxy.curator.prepper import run_prepper
    with patch("archolith_proxy.curator.prepper.get_settings") as mock_settings:
        settings = MagicMock()
        settings.prepper_api_key = ""
        settings.curator_api_key = ""
        settings.extractor_api_key = ""
        mock_settings.return_value = settings
        result = await run_prepper(
            session_id="test", turn_number=1,
            user_message="hello", session_goal="test goal",
            messages=[],
        )
        assert result is None


@pytest.mark.asyncio
async def test_run_prepper_timeout_returns_none():
    """run_prepper() returns None on timeout."""
    import asyncio

    from archolith_proxy.curator.prepper import run_prepper
    with patch("archolith_proxy.curator.prepper.get_settings") as mock_settings:
        settings = MagicMock()
        settings.prepper_api_key = "test-key"
        settings.prepper_base_url = "https://test.com/v1"
        settings.prepper_model = "test-model"
        settings.curator_api_key = ""
        settings.curator_base_url = ""
        settings.curator_model = ""
        settings.extractor_api_key = "test-key"
        settings.prepper_latency_budget_ms = 100
        settings.prepper_max_iterations = 1
        settings.coherence_tail_size = 3
        settings.max_tail_messages = 10
        mock_settings.return_value = settings

        # Mock AsyncOpenAI to simulate timeout
        with patch("archolith_proxy.curator.prepper.AsyncOpenAI") as mock_openai_cls:
            mock_openai_cls.return_value = MagicMock()
            with patch("archolith_proxy.curator.prepper.asyncio.wait_for",
                        side_effect=asyncio.TimeoutError()):
                with patch("archolith_proxy.curator.prepper.get_snapshot", return_value=None):
                    with patch("archolith_proxy.curator.prepper.build_curator_user_prompt", return_value="prompt"):
                        result = await run_prepper(
                            session_id="test", turn_number=1,
                            user_message="hello", session_goal="test goal",
                            messages=[],
                        )
                        assert result is None


# ---------------------------------------------------------------------------
# Assembler module tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_assembler_no_api_key():
    """run_assembler() returns None when no API key is configured."""
    from archolith_proxy.curator.assembler import run_assembler
    with patch("archolith_proxy.curator.assembler.get_settings") as mock_settings:
        settings = MagicMock()
        settings.assembler_api_key = ""
        settings.curator_api_key = ""
        settings.extractor_api_key = ""
        settings.assembler_model = ""
        settings.curator_model = ""
        settings.extractor_model = ""
        settings.assembler_base_url = ""
        settings.curator_base_url = ""
        settings.extractor_base_url = ""
        mock_settings.return_value = settings

        client = MagicMock()
        briefing = MagicMock()
        result = await run_assembler(
            session_id="test", turn_number=2,
            user_message="hello", session_goal="test",
            briefing=briefing, messages=[], client=client,
            model="test-model", settings=settings,
        )
        assert result is None
