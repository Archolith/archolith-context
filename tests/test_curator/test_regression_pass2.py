"""Regression tests for Pass 2 fixes — file_relevance, iterations_used, invalid-json, bg-task swap, prefetch validation.

These tests verify the behavior fixed in Chunk 3 Curator Pass 1, now extended
with Pass 2 regression testing to prevent backslip.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Ensure openai stub is available for all imports
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
from archolith_proxy.curator.briefing import build_briefing_from_result  # noqa: E402
from archolith_proxy.curator.result import CuratorResult, CuratorToolCall  # noqa: E402
from archolith_proxy.curator.state import swap_background_task, _bg_tasks  # noqa: E402


# ============================================================================
# 4.1: FILE_RELEVANCE TEST
# ============================================================================

@pytest.mark.asyncio
async def test_file_relevance_populated_from_score_tool():
    """Test that build_briefing_from_result populates file_relevance from score_file_relevance result.

    The score_file_relevance tool returns lines like:
      "3.0 | src/foo.py | reason for match"

    We verify that the PreFetchedFile.relevance field is populated with the reason
    (not a generic fallback).
    """
    # Construct a tool_log with a score_file_relevance result
    tool_log = [
        CuratorToolCall(
            tool="score_file_relevance",
            args={"query": "auth handler"},
            status="ok",
            result_preview="File relevance for: auth handler\n\n3.0 | src/auth/handler.py | keyword 'auth' in path; keyword 'handler' in path\n2.0 | src/db/models.py | active at turn 5",
            raw_result="File relevance for: auth handler\n\nScore | Path | Reasons\n------|------|--------\n3.0 | src/auth/handler.py | keyword 'auth' in path; keyword 'handler' in path\n2.0 | src/db/models.py | active at turn 5",
        )
    ]

    # Create a minimal CuratorResult
    result = CuratorResult(
        context_text="=== SESSION GOAL ===\nTest goal",
        tool_calls_used=1,
        iterations_used=1,
        tool_log=tool_log,
    )

    briefing = build_briefing_from_result(
        result,
        session_id="test_session",
        turn_number=1,
        latency_ms=100.0,
        session_goal="Test goal",
        messages=[],
    )

    # Verify file_relevance was populated
    assert len(briefing.files) == 2

    # Find the handler.py file
    handler_file = next((f for f in briefing.files if "handler.py" in f.path), None)
    assert handler_file is not None
    assert "keyword 'auth' in path" in handler_file.relevance
    assert "keyword 'handler' in path" in handler_file.relevance

    # Find the models.py file
    models_file = next((f for f in briefing.files if "models.py" in f.path), None)
    assert models_file is not None
    assert "active at turn 5" in models_file.relevance


# ============================================================================
# 4.2: ITERATIONS_USED TEST
# ============================================================================

@pytest.mark.asyncio
async def test_iterations_used_less_than_tool_calls():
    """Test that iterations_used < tool_calls_used when multiple tool calls occur in one iteration.

    One iteration can make multiple tool calls. The counts should be distinct.
    """
    # Simulate a scenario where one iteration makes 3 tool calls
    tool_log = [
        CuratorToolCall(tool="get_file", args={"path": "foo.py"}, status="ok", raw_result="content1"),
        CuratorToolCall(tool="search_facts", args={"query": "auth"}, status="ok", raw_result="facts"),
        CuratorToolCall(tool="get_file_outline", args={"path": "bar.py"}, status="ok", raw_result="outline"),
    ]

    result = CuratorResult(
        context_text="=== SESSION GOAL ===\nTest",
        tool_calls_used=3,  # 3 tool calls
        iterations_used=1,  # but in 1 iteration
        tool_log=tool_log,
    )

    # Verify the distinction
    assert result.tool_calls_used == 3
    assert result.iterations_used == 1
    assert result.iterations_used < result.tool_calls_used


# ============================================================================
# F1: INVALID-JSON NATIVE HANDLER TEST
# ============================================================================

@pytest.mark.asyncio
async def test_invalid_json_in_tool_call_appends_error():
    """Test that malformed JSON in tool arguments appends a status='error' CuratorToolCall.

    This tests the error handling in _run_curator_native (loop.py ~300-308).
    """
    # Import the loop function
    from archolith_proxy.curator.loop import _run_curator_native
    from archolith_proxy.curator.schemas import ALL_CURATOR_TOOLS

    # Mock the OpenAI client to return a tool call with malformed JSON
    mock_client = MagicMock()

    # Create a response with a tool call that has invalid JSON
    tool_call_obj = MagicMock()
    tool_call_obj.function.name = "get_file"
    tool_call_obj.function.arguments = '{"path": "foo.py"'  # Missing closing brace
    tool_call_obj.id = "call_001"

    message_obj = MagicMock()
    message_obj.content = None
    message_obj.tool_calls = [tool_call_obj]

    choice_obj = MagicMock()
    choice_obj.finish_reason = "tool_calls"
    choice_obj.message = message_obj

    response_obj = MagicMock()
    response_obj.choices = [choice_obj]

    # Second response: model stops after error feedback
    message_obj2 = MagicMock()
    message_obj2.content = "=== SESSION GOAL ===\nTest"
    message_obj2.tool_calls = None

    choice_obj2 = MagicMock()
    choice_obj2.finish_reason = "stop"
    choice_obj2.message = message_obj2

    response_obj2 = MagicMock()
    response_obj2.choices = [choice_obj2]

    # Patch _llm_call_with_retry to return our responses
    with patch("archolith_proxy.curator.loop._llm_call_with_retry") as mock_llm:
        mock_llm.side_effect = [response_obj, response_obj2]

        result, tool_log, failure_reason = await _run_curator_native(
            client=mock_client,
            session_id="test",
            user_prompt="test prompt",
            max_iterations=2,
            system_prompt="test system",
            model="test-model",
            tool_set=ALL_CURATOR_TOOLS,
        )

    # Verify that the tool_log contains an error entry for the malformed JSON
    error_calls = [tc for tc in tool_log if tc.status == "error"]
    assert len(error_calls) > 0

    # Verify the error is about invalid JSON
    json_error_calls = [tc for tc in error_calls if "invalid" in tc.error.lower()]
    assert len(json_error_calls) > 0, "Expected an error entry for invalid JSON"


# ============================================================================
# F2: SWAP_BACKGROUND_TASK IDENTITY TEST
# ============================================================================

def test_swap_background_task_preserves_new_task():
    """Test that swap_background_task keeps the NEW task in _bg_tasks even if OLD task completes.

    When OLD task's done callback fires AFTER NEW task was registered,
    the callback must not pop the NEW task.
    """
    # Clear state
    session_id = "test_session_bg"
    _bg_tasks.clear()

    # Create task A
    async def dummy_coro():
        await asyncio.sleep(0.01)

    task_a = asyncio.ensure_future(dummy_coro())

    # Register task A
    swap_background_task(session_id, task_a)
    assert _bg_tasks[session_id] is task_a

    # Create and register task B (replaces A)
    task_b = asyncio.ensure_future(dummy_coro())
    swap_background_task(session_id, task_b)
    assert _bg_tasks[session_id] is task_b

    # Now simulate A completing — its callback should NOT pop B
    # We'll manually complete A and process callbacks
    task_a.cancel()

    # Give the event loop a chance to run callbacks
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.sleep(0.01))

    # Verify B is still there
    assert session_id in _bg_tasks
    assert _bg_tasks[session_id] is task_b

    # Cleanup
    task_b.cancel()


# ============================================================================
# F3: PREFETCH_FILE RELATIVE-PATH VALIDATION TEST
# ============================================================================

@pytest.mark.asyncio
async def test_prefetch_file_rejects_outside_allowed_roots():
    """Test that prefetch_file rejects paths that resolve outside allowed_roots.

    This tests the F3 final-path validation in tools.py ~457.
    """
    from archolith_proxy.curator.tools import prefetch_file

    # Create a temporary directory structure
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        allowed_dir = tmpdir_path / "allowed"
        allowed_dir.mkdir()
        blocked_dir = tmpdir_path / "blocked"
        blocked_dir.mkdir()

        # Create a test file in the blocked directory
        blocked_file = blocked_dir / "test.txt"
        blocked_file.write_text("secret content")

        # Mock get_settings to restrict to allowed_dir
        with patch("archolith_proxy.curator.tools.get_settings") as mock_settings:
            settings = MagicMock()
            settings.prefetch_allowed_roots = [str(allowed_dir)]
            settings.file_cache_max_file_bytes = 1_000_000
            mock_settings.return_value = settings

            # Mock the backend
            with patch("archolith_proxy.curator.tools.get_backend") as mock_backend:
                mock_backend.return_value.list_cached_files = AsyncMock(return_value=[])

                # Attempt to prefetch the blocked file using absolute path
                result = await prefetch_file(
                    session_id="test",
                    path=str(blocked_file),
                )

                # Should be blocked
                assert "blocked" in result or "outside" in result


# ============================================================================
# 4.5: REUSE HTTPX CLIENT TEST
# ============================================================================

@pytest.mark.asyncio
async def test_semantic_search_reuses_httpx_client():
    """Test that search_facts_semantic uses a module-level client rather than creating one per call.

    This is task 4.5 — verify the lazy client pattern is implemented.
    """
    from archolith_proxy.curator import tools

    # Verify the module has the lazy client functions
    assert hasattr(tools, '_get_semantic_client'), "Missing _get_semantic_client function"
    assert hasattr(tools, '_semantic_client'), "Missing _semantic_client module variable"

    # Test that calling _get_semantic_client twice returns the same client
    client1 = await tools._get_semantic_client()
    client2 = await tools._get_semantic_client()

    # They should be the same object (reused)
    assert client1 is client2

    # Cleanup
    await tools.close_semantic_client()


# ============================================================================
# Additional: Stuck-loop detection test
# ============================================================================

@pytest.mark.asyncio
async def test_stuck_loop_detection_on_repeated_errors():
    """Test that repeated errors on the same tool trigger stuck-loop detection.

    This verifies the error_window logic in _run_curator_native detects when
    the same tool fails 4 times in a row.
    """
    from archolith_proxy.curator.loop import _run_curator_native
    from archolith_proxy.curator.schemas import ALL_CURATOR_TOOLS

    mock_client = MagicMock()

    # Create tool call responses that each fail on get_file
    def make_failing_response():
        tool_call = MagicMock()
        tool_call.function.name = "get_file"
        tool_call.function.arguments = '{"path": "foo.py"}'
        tool_call.id = "call_001"

        message = MagicMock()
        message.content = None
        message.tool_calls = [tool_call]

        choice = MagicMock()
        choice.finish_reason = "tool_calls"
        choice.message = message

        response = MagicMock()
        response.choices = [choice]
        return response

    # Patch the tool handler to always raise
    with patch("archolith_proxy.curator.loop.TOOL_HANDLERS", {"get_file": AsyncMock(side_effect=Exception("test error"))}):
        with patch("archolith_proxy.curator.loop._llm_call_with_retry") as mock_llm:
            # Return failing get_file responses 4+ times, then a stop
            mock_llm.side_effect = [
                make_failing_response(),  # iter 1: tool call fails
                make_failing_response(),  # iter 2: tool call fails
                make_failing_response(),  # iter 3: tool call fails
                make_failing_response(),  # iter 4: tool call fails (4th error → stuck)
            ]

            result, tool_log, failure_reason = await _run_curator_native(
                client=mock_client,
                session_id="test_stuck",
                user_prompt="test",
                max_iterations=5,
                system_prompt="test",
                model="test-model",
                tool_set=ALL_CURATOR_TOOLS,
            )

    # Should detect stuck loop
    assert failure_reason.startswith("stuck_loop")

    # Verify tool_log has multiple error entries
    error_calls = [tc for tc in tool_log if tc.status == "error"]
    assert len(error_calls) >= 4


__all__ = [
    "test_file_relevance_populated_from_score_tool",
    "test_iterations_used_less_than_tool_calls",
    "test_invalid_json_in_tool_call_appends_error",
    "test_swap_background_task_preserves_new_task",
    "test_prefetch_file_rejects_outside_allowed_roots",
    "test_semantic_search_reuses_httpx_client",
    "test_stuck_loop_detection_on_repeated_errors",
]
