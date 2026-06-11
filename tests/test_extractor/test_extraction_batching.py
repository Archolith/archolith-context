"""Tests for turn-boundary extraction batching.

Verifies:
  - _is_turn_boundary truth table
  - Agent-solo continuation in turn_boundary mode performs zero extractor LLM calls
  - File cache capture still runs on continuation turns
  - finish_reason=stop and is_user_turn both trigger full extraction
  - extraction_mode=every_turn preserves current behavior
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archolith_proxy.openai.extraction import _is_turn_boundary


# ---------------------------------------------------------------------------
# _is_turn_boundary — truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_user_turn", "finish_reason", "expected"),
    [
        (True, None, True),
        (True, "", True),
        (True, "stop", True),
        (True, "tool_calls", True),
        (True, "length", True),
        (False, "stop", True),
        (False, None, False),
        (False, "", False),
        (False, "tool_calls", False),
        (False, "length", False),
        (False, "content_filter", False),
    ],
)
def test_is_turn_boundary_truth_table(
    is_user_turn: bool, finish_reason: str | None, expected: bool
) -> None:
    """_is_turn_boundary returns True only when is_user_turn or finish_reason=stop."""
    assert _is_turn_boundary(is_user_turn, finish_reason) is expected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.extraction_mode = "turn_boundary"
    settings.file_cache_enabled = True
    settings.per_tool_extraction_enabled = False
    settings.promotion_enabled = False
    settings.background_pass_enabled = False
    settings.curator_enabled = False
    return settings


@pytest.fixture
def mock_lock() -> AsyncMock:
    lock = AsyncMock()
    lock.acquire = AsyncMock()
    return lock


def _make_empty_result():
    """Return a minimal extraction result with no facts."""
    r = MagicMock()
    r.facts = []
    r.session_goal = None
    r.files_touched = []
    r.decisions = []
    r.checkpoint = None
    r.issues = []
    r.verifications = []
    r.invalidated_fact_ids = []
    return r


def _start_base_patches(mock_settings):
    """Start common patches and return the list for cleanup."""
    patches = [
        patch("archolith_proxy.openai.extraction.get_settings", return_value=mock_settings),
        patch("archolith_proxy.openai.extraction.get_backend", return_value=AsyncMock()),
        patch("archolith_proxy.openai.extraction.record_metric", MagicMock()),
        patch(
            "archolith_proxy.openai.extraction._normalize_message_content",
            side_effect=lambda x: x or "",
        ),
        patch("archolith_proxy.openai.extraction.strip_reasoning", side_effect=lambda x: x),
    ]
    for p in patches:
        p.start()
    return patches


# ---------------------------------------------------------------------------
# Extraction gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_boundary_skips_llm_on_agent_solo(
    mock_settings: MagicMock, mock_lock: AsyncMock
) -> None:
    """Agent-solo continuation should not call extract_facts."""
    from archolith_proxy.openai.extraction import _run_extraction

    base = _start_base_patches(mock_settings)
    extract_patch = patch(
        "archolith_proxy.openai.extraction.extract_facts", new_callable=MagicMock,
    )
    mock_extract = extract_patch.start()
    embed_patch = patch(
        "archolith_proxy.openai.extraction._compute_fact_embeddings", return_value=[],
    )
    embed_patch.start()

    try:
        with patch("archolith_proxy.proxy.locks.get_session_lock", return_value=mock_lock):
            client = MagicMock()
            await _run_extraction(
                client=client,
                session_id="test-session",
                turn_number=3,
                messages=[{"role": "user", "content": "hello"}],
                response_text="world",
                is_user_turn=False,
                response_finish_reason=None,
            )

        mock_extract.assert_not_called()
    finally:
        for p in base:
            p.stop()
        extract_patch.stop()
        embed_patch.stop()


@pytest.mark.asyncio
async def test_turn_boundary_runs_llm_on_user_turn(
    mock_settings: MagicMock, mock_lock: AsyncMock
) -> None:
    """User turn must call extract_facts."""
    from archolith_proxy.openai.extraction import _run_extraction

    mock_result = _make_empty_result()
    base = _start_base_patches(mock_settings)
    mock_extract = patch(
        "archolith_proxy.openai.extraction.extract_facts", return_value=mock_result
    ).start()
    patch(
        "archolith_proxy.openai.extraction._compute_fact_embeddings", return_value=[]
    ).start()

    try:
        with patch("archolith_proxy.proxy.locks.get_session_lock", return_value=mock_lock):
            client = MagicMock()
            await _run_extraction(
                client=client,
                session_id="test-session",
                turn_number=1,
                messages=[{"role": "user", "content": "hello"}],
                response_text="world",
                is_user_turn=True,
                response_finish_reason="stop",
            )

        mock_extract.assert_called()
    finally:
        for p in base:
            p.stop()


@pytest.mark.asyncio
async def test_turn_boundary_runs_llm_on_finish_stop(
    mock_settings: MagicMock, mock_lock: AsyncMock
) -> None:
    """finish_reason=stop triggers full extraction even without user turn."""
    from archolith_proxy.openai.extraction import _run_extraction

    mock_result = _make_empty_result()
    base = _start_base_patches(mock_settings)
    mock_extract = patch(
        "archolith_proxy.openai.extraction.extract_facts", return_value=mock_result
    ).start()
    patch(
        "archolith_proxy.openai.extraction._compute_fact_embeddings", return_value=[]
    ).start()

    try:
        with patch("archolith_proxy.proxy.locks.get_session_lock", return_value=mock_lock):
            client = MagicMock()
            await _run_extraction(
                client=client,
                session_id="test-session",
                turn_number=5,
                messages=[{"role": "tool", "content": "done"}],
                response_text="task complete",
                is_user_turn=False,
                response_finish_reason="stop",
            )

        mock_extract.assert_called()
    finally:
        for p in base:
            p.stop()


@pytest.mark.asyncio
async def test_file_cache_runs_every_turn(
    mock_settings: MagicMock, mock_lock: AsyncMock
) -> None:
    """File cache capture must run even on non-boundary turns."""
    from archolith_proxy.openai.extraction import _run_extraction

    base = _start_base_patches(mock_settings)

    fcc_patch = patch(
        "archolith_proxy.openai.extraction._run_file_cache_capture",
        new_callable=AsyncMock,
    )
    fcc_mock = fcc_patch.start()

    try:
        with patch("archolith_proxy.proxy.locks.get_session_lock", return_value=mock_lock):
            client = MagicMock()
            await _run_extraction(
                client=client,
                session_id="test-session",
                turn_number=2,
                messages=[{"role": "tool", "content": "result"}],
                response_text="reply",
                is_user_turn=False,
                response_finish_reason=None,
            )

        fcc_mock.assert_awaited_once()
    finally:
        for p in base:
            p.stop()
        fcc_patch.stop()


@pytest.mark.asyncio
async def test_every_turn_mode_preserves_behavior(
    mock_settings: MagicMock, mock_lock: AsyncMock
) -> None:
    """With extraction_mode=every_turn, extraction runs on all turns."""
    from archolith_proxy.openai.extraction import _run_extraction

    mock_settings.extraction_mode = "every_turn"
    mock_result = _make_empty_result()
    base = _start_base_patches(mock_settings)
    mock_extract = patch(
        "archolith_proxy.openai.extraction.extract_facts", return_value=mock_result
    ).start()
    patch(
        "archolith_proxy.openai.extraction._compute_fact_embeddings", return_value=[]
    ).start()

    try:
        with patch("archolith_proxy.proxy.locks.get_session_lock", return_value=mock_lock):
            client = MagicMock()
            await _run_extraction(
                client=client,
                session_id="test-session",
                turn_number=4,
                messages=[{"role": "tool", "content": "continuation"}],
                response_text="output",
                is_user_turn=False,
                response_finish_reason=None,
            )

        mock_extract.assert_called()
    finally:
        for p in base:
            p.stop()
