"""Tests for _wrap_response_as_sse — non-streaming to SSE conversion.

Covers:
1. Content-only responses (the original working path)
2. Tool-call-only responses (finish_reason=tool_calls, no content)
3. Mixed content + tool_calls responses (the regression scenario)
4. Error propagation (status >= 400)
5. Multiple tool calls in a single response
6. Circuit breaker integration
7. Per-session token budget
"""

import asyncio
import json

from starlette.responses import Response

from archolith_proxy.proxy.streaming import _wrap_response_as_sse
from archolith_proxy.proxy.circuit_breaker import (
    add_session_tokens,
    get_circuit_state,
    is_session_over_budget,
    is_synthetic_allowed,
    record_synthetic_failure,
    record_synthetic_success,
    reset_all,
    reset_circuit,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _collect_sse_chunks(response) -> list[dict]:
    """Consume the streaming response and return parsed SSE data chunks."""
    # The inner generator is accessible via the body_iterator
    chunks = []

    async def _collect():
        async for line in response.body_iterator:
            stripped = line.strip()
            if not stripped or stripped == "data: [DONE]":
                continue
            if stripped.startswith("data: "):
                data = json.loads(stripped[6:])
                chunks.append(data)

    asyncio.get_event_loop().run_until_complete(_collect())
    return chunks


def _make_response(data: dict, status_code: int = 200) -> Response:
    """Create a Starlette Response with JSON body."""
    return Response(
        content=json.dumps(data),
        status_code=status_code,
        media_type="application/json",
    )


def _basic_response(content: str = "Hello!", finish_reason: str = "stop") -> dict:
    """Build a basic non-streaming chat completion response."""
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "deepseek-chat",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
    }


def _tool_call_response(
    tool_calls: list[dict],
    content: str | None = None,
    finish_reason: str = "tool_calls",
) -> dict:
    """Build a response containing tool calls."""
    message = {"role": "assistant"}
    if content:
        message["content"] = content
    else:
        message["content"] = None
    message["tool_calls"] = tool_calls

    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "deepseek-chat",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
    }


# ── SSE Content Tests ───────────────────────────────────────────────────────

class TestWrapResponseAsSseContent:
    """Test content-only SSE conversion (original working path)."""

    def test_content_only_produces_role_content_finish(self):
        resp = _make_response(_basic_response("Hello world"))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        assert len(chunks) == 3  # role, content, finish
        # Role delta
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        # Content delta
        assert chunks[1]["choices"][0]["delta"]["content"] == "Hello world"
        # Finish delta
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"

    def test_empty_content_omits_content_delta(self):
        resp = _make_response(_basic_response("", "stop"))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        # Should have role + finish only (no content delta for empty string)
        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[1]["choices"][0]["finish_reason"] == "stop"


# ── SSE Tool Calls Tests ────────────────────────────────────────────────────

class TestWrapResponseAsSseToolCalls:
    """Test tool_calls SSE conversion (the regression fix)."""

    def test_single_tool_call_produces_name_args_finish(self):
        """A single tool call should emit: role, name delta, args delta, finish."""
        tool_calls = [{
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{\"path\": \"/foo\"}"},
        }]
        resp = _make_response(_tool_call_response(tool_calls))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        # Expected: role, name delta, args delta, finish = 4 chunks
        assert len(chunks) == 4

        # Role delta
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"

        # Name delta — must include index, id, type, function.name
        tc_delta = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
        assert tc_delta["index"] == 0
        assert tc_delta["id"] == "call_abc123"
        assert tc_delta["type"] == "function"
        assert tc_delta["function"]["name"] == "read_file"
        assert tc_delta["function"]["arguments"] == ""

        # Arguments delta — must include index and function.arguments
        args_delta = chunks[2]["choices"][0]["delta"]["tool_calls"][0]
        assert args_delta["index"] == 0
        assert args_delta["function"]["arguments"] == "{\"path\": \"/foo\"}"

        # Finish delta
        assert chunks[3]["choices"][0]["finish_reason"] == "tool_calls"

    def test_two_tool_calls_produce_separate_deltas(self):
        """Multiple tool calls should each get their own name + args deltas."""
        tool_calls = [
            {"id": "call_0", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"/a\"}"}},
            {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": "{\"path\": \"/b\", \"content\": \"x\"}"}},
        ]
        resp = _make_response(_tool_call_response(tool_calls))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        # Expected: role, name0, args0, name1, args1, finish = 6 chunks
        assert len(chunks) == 6

        # First tool call name
        tc0_name = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
        assert tc0_name["index"] == 0
        assert tc0_name["function"]["name"] == "read_file"

        # First tool call args
        tc0_args = chunks[2]["choices"][0]["delta"]["tool_calls"][0]
        assert tc0_args["index"] == 0
        assert tc0_args["function"]["arguments"] == "{\"path\": \"/a\"}"

        # Second tool call name
        tc1_name = chunks[3]["choices"][0]["delta"]["tool_calls"][0]
        assert tc1_name["index"] == 1
        assert tc1_name["function"]["name"] == "write_file"

        # Second tool call args
        tc1_args = chunks[4]["choices"][0]["delta"]["tool_calls"][0]
        assert tc1_args["index"] == 1

        # Finish delta
        assert chunks[5]["choices"][0]["finish_reason"] == "tool_calls"

    def test_mixed_content_and_tool_calls(self):
        """Content + tool_calls — the mixed-call regression scenario."""
        tool_calls = [{
            "id": "call_mixed",
            "type": "function",
            "function": {"name": "search", "arguments": "{\"q\": \"test\"}"},
        }]
        data = _tool_call_response(tool_calls, content="I'll search for that.")
        resp = _make_response(data)
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        # Expected: role, content, name, args, finish = 5 chunks
        assert len(chunks) == 5
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[1]["choices"][0]["delta"]["content"] == "I'll search for that."
        assert chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "search"
        assert chunks[4]["choices"][0]["finish_reason"] == "tool_calls"

    def test_tool_call_without_arguments(self):
        """Tool call with empty arguments — should skip args delta."""
        tool_calls = [{
            "id": "call_noargs",
            "type": "function",
            "function": {"name": "list_files", "arguments": ""},
        }]
        resp = _make_response(_tool_call_response(tool_calls))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        # Expected: role, name delta, finish (no args delta since arguments is empty)
        assert len(chunks) == 3
        assert chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "list_files"

    def test_tool_call_missing_id_gets_default(self):
        """Tool call without id should get a generated default."""
        tool_calls = [{
            "type": "function",
            "function": {"name": "ping", "arguments": "{}"},
        }]
        resp = _make_response(_tool_call_response(tool_calls))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        tc_delta = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
        assert tc_delta["id"] == "call_0"  # Default: call_{index}

    def test_sse_chunks_have_valid_structure(self):
        """Every SSE chunk must have id, object, model, created, choices."""
        tool_calls = [{
            "id": "call_struct",
            "type": "function",
            "function": {"name": "test_fn", "arguments": "{\"k\": 1}"},
        }]
        resp = _make_response(_tool_call_response(tool_calls))
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        for chunk in chunks:
            assert "id" in chunk
            assert chunk["object"] == "chat.completion.chunk"
            assert "model" in chunk
            assert "created" in chunk
            assert "choices" in chunk
            assert len(chunk["choices"]) == 1
            assert "index" in chunk["choices"][0]


# ── SSE Error Propagation Tests ──────────────────────────────────────────────

class TestWrapResponseAsSseErrors:
    """Test error propagation for status >= 400."""

    def test_error_response_propagated_as_sse(self):
        error_data = {"error": {"message": "Rate limited", "type": "rate_limit_error"}}
        resp = _make_response(error_data, status_code=429)
        sse = _wrap_response_as_sse(resp)
        chunks = _collect_sse_chunks(sse)

        # Should have the error data and nothing else
        assert len(chunks) == 1
        assert chunks[0]["error"]["message"] == "Rate limited"


# ── Circuit Breaker Tests ───────────────────────────────────────────────────

class TestCircuitBreaker:
    """Test per-session circuit breaker for synthetic tools."""

    def setup_method(self):
        reset_all()

    def test_initial_state_allows_injection(self):
        assert is_synthetic_allowed("sess-1") is True

    def test_consecutive_failures_open_circuit(self):
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        assert is_synthetic_allowed("sess-1") is True  # 2 failures, not yet 3

        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        assert is_synthetic_allowed("sess-1") is False  # 3 consecutive → circuit open

    def test_success_resets_consecutive_counter(self):
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_success("sess-1")  # Reset consecutive
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        assert is_synthetic_allowed("sess-1") is True  # Only 1 consecutive again

    def test_total_failures_hard_disable(self):
        for _ in range(10):
            record_synthetic_failure("sess-1", max_consecutive=100, cooldown_seconds=0.1, max_total=10)
        state = get_circuit_state("sess-1")
        assert state.hard_disabled is True
        assert is_synthetic_allowed("sess-1") is False

    def test_independent_sessions(self):
        record_synthetic_failure("sess-A", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_failure("sess-A", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_failure("sess-A", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        assert is_synthetic_allowed("sess-A") is False
        assert is_synthetic_allowed("sess-B") is True  # Independent

    def test_reset_circuit(self):
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        record_synthetic_failure("sess-1", max_consecutive=3, cooldown_seconds=300.0, max_total=10)
        reset_circuit("sess-1")
        assert is_synthetic_allowed("sess-1") is True


# ── Token Budget Tests ──────────────────────────────────────────────────────

class TestTokenBudget:
    """Test per-session token budget tracking."""

    def setup_method(self):
        reset_all()

    def test_unlimited_budget(self):
        add_session_tokens("sess-1", 10_000_000)
        assert is_session_over_budget("sess-1", max_tokens=0) is False  # 0 = unlimited

    def test_over_budget(self):
        add_session_tokens("sess-1", 1_500_000)
        assert is_session_over_budget("sess-1", max_tokens=2_000_000) is False
        add_session_tokens("sess-1", 600_000)
        assert is_session_over_budget("sess-1", max_tokens=2_000_000) is True

    def test_cumulative_tracking(self):
        for _ in range(20):
            add_session_tokens("sess-1", 100_000)
        state = get_circuit_state("sess-1")
        assert state.total_input_tokens == 2_000_000
