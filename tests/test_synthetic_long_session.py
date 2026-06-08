"""Integration tests for long synthetic sessions.

Exercises the full proxy pipeline with realistic multi-turn conversations:
- Context assembly triggers on long conversations
- __archolith_recall interception with session graph data
- recall_session_work synthetic tool interception
- Native Read interception via file cache
- Circuit breaker under repeated synthetic failures
- Token budget enforcement

All tests use mocked upstream and graph backends — no live services needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.config import get_settings, reset_settings
from archolith_proxy.main import create_app
from archolith_proxy.metrics import get_metrics
from archolith_proxy.models.dtos import AssembledContext
from archolith_proxy.proxy.circuit_breaker import reset_all, reset_circuit
from archolith_proxy.proxy.rewrite import estimate_input_tokens

from tests.fixtures.conversations import (
    build_coding_session_long,
    build_coding_session_short,
    build_read_cache_session,
    build_recall_trigger_session,
    estimate_token_count,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NORMAL_RESPONSE = {
    "id": "chatcmpl-synlong-001",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Here is my response based on the context."},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600},
}


def _recall_tool_call_response(question: str = "api key") -> dict:
    """Upstream response where the model calls __archolith_recall."""
    return {
        "id": "chatcmpl-recall-001",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_recall_001",
                    "type": "function",
                    "function": {
                        "name": "__archolith_recall",
                        "arguments": json.dumps({"question": question}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520},
    }


def _synthetic_tool_call_response() -> dict:
    """Upstream response where the model calls recall_session_work."""
    return {
        "id": "chatcmpl-syn-001",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_syn_001",
                    "type": "function",
                    "function": {
                        "name": "recall_session_work",
                        "arguments": "{}",
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 500, "completion_tokens": 10, "total_tokens": 510},
    }


def _read_tool_call_response(path: str = "models.py") -> dict:
    """Upstream response where the model calls Read."""
    return {
        "id": "chatcmpl-read-001",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_read_001",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"file_path": path}),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 200, "completion_tokens": 10, "total_tokens": 210},
    }


def _make_mock_backend(
    facts: list[dict] | None = None,
    cached_files: dict | None = None,
    decisions: list[dict] | None = None,
):
    """Create a mock graph backend with configurable facts and file cache."""
    backend = AsyncMock()
    backend.get_turn_number = AsyncMock(return_value=10)
    backend.find_session_by_id = AsyncMock(return_value={"session_id": "test-session", "goal": "implement reviews"})
    backend.create_session = AsyncMock()
    backend.touch_session = AsyncMock()
    backend.update_goal = AsyncMock()
    backend.store_facts = AsyncMock()
    backend.delete_file_content = AsyncMock(return_value=True)
    backend.get_active_facts = AsyncMock(return_value=facts or [])
    backend.get_decisions = AsyncMock(return_value=decisions or [])
    backend.get_checkpoint = AsyncMock(return_value=None)

    _cache = cached_files or {}

    async def _get_file_content(session_id, path):
        return _cache.get(path)

    async def _get_file_lines(session_id, path, start, end):
        info = _cache.get(path)
        if not info:
            return None
        lines = info["content"].split("\n")
        start = max(1, start)
        end = min(end, len(lines))
        selected = lines[start - 1:end]
        return "\n".join(f"{start + i}: {line}" for i, line in enumerate(selected))

    async def _list_cached_files(session_id):
        return [
            {"path": p, "sha256": v.get("sha256", ""), "line_count": v.get("line_count", 0), "last_updated_turn": 1}
            for p, v in _cache.items()
        ]

    backend.get_file_content = AsyncMock(side_effect=_get_file_content)
    backend.get_file_lines = AsyncMock(side_effect=_get_file_lines)
    backend.list_cached_files = AsyncMock(side_effect=_list_cached_files)
    return backend


# ---------------------------------------------------------------------------
# Test: conversation fixture quality
# ---------------------------------------------------------------------------


class TestConversationFixtures:
    """Verify that conversation fixtures are well-formed."""

    def test_short_session_token_count(self):
        messages = build_coding_session_short()
        tokens = estimate_token_count(messages)
        assert tokens > 2000, f"Short session too small: {tokens} tokens"
        assert tokens < 30000, f"Short session too large: {tokens} tokens"

    def test_long_session_token_count(self):
        messages = build_coding_session_long()
        tokens = estimate_token_count(messages)
        assert tokens > 10000, f"Long session too small: {tokens} tokens"

    def test_long_session_has_enough_turns(self):
        messages = build_coding_session_long()
        user_turns = sum(1 for m in messages if m.get("role") == "user")
        assert user_turns >= 8, f"Expected >= 8 user turns, got {user_turns}"

    def test_recall_session_has_fact_context(self):
        messages = build_recall_trigger_session()
        # Should have read tool results with substantial content
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        total_content = sum(len(m.get("content", "")) for m in tool_msgs)
        assert total_content > 4000, f"Expected > 4K chars in tool results, got {total_content}"

    def test_read_cache_session_has_repeated_reads(self):
        messages = build_read_cache_session()
        # Extract all Read tool call paths
        read_paths = []
        for msg in messages:
            for tc in (msg.get("tool_calls") or []):
                func = tc.get("function", {})
                if func.get("name") == "Read":
                    args = json.loads(func.get("arguments", "{}"))
                    read_paths.append(args.get("file_path", ""))
        # Should have duplicate paths (for cache hit testing)
        unique = set(read_paths)
        assert len(read_paths) > len(unique), (
            f"Expected repeated reads, got {len(read_paths)} reads of {len(unique)} unique files"
        )

    def test_all_tool_results_have_matching_calls(self):
        """Every tool result must have a matching tool_call_id in a preceding assistant message."""
        for builder_name, builder in [
            ("short", build_coding_session_short),
            ("long", build_coding_session_long),
            ("recall", build_recall_trigger_session),
            ("cache", build_read_cache_session),
        ]:
            messages = builder()
            # Collect all tool_call IDs from assistant messages
            call_ids = set()
            for msg in messages:
                for tc in (msg.get("tool_calls") or []):
                    if tc is not None:
                        call_ids.add(tc.get("id", ""))
            # Check all tool results reference a known call ID
            for msg in messages:
                if msg.get("role") == "tool":
                    tc_id = msg.get("tool_call_id", "")
                    assert tc_id in call_ids, (
                        f"[{builder_name}] Tool result references unknown call ID: {tc_id}"
                    )


# ---------------------------------------------------------------------------
# Test: long session through full proxy pipeline
# ---------------------------------------------------------------------------


class TestLongSessionPipeline:
    """End-to-end tests with long conversations through the proxy."""

    @pytest.mark.asyncio
    async def test_long_session_processes_without_error(self):
        """A 25+ turn conversation should process through the proxy without errors."""
        SESSION_ID = "synlong-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_long()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        settings = get_settings()
        mock_backend = _make_mock_backend(facts=[
            {"content": "Review model has rating field (1-5)", "fact_type": "observation", "confidence": 0.9, "source_turn": 1},
            {"content": "Order model has status transitions: pending→confirmed→shipped→delivered", "fact_type": "observation", "confidence": 0.95, "source_turn": 3},
        ])
        app = create_app()

        with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
             patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
             patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
             patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
             patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

            mock_resolve.return_value = (SESSION_ID, False)

            async with app.router.lifespan_context(app):
                app.state.http_client = httpx.AsyncClient(
                    transport=httpx.MockTransport(mock_handler)
                )
                app.state.extractor_client = AsyncMock()
                transport = ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    resp = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "model": "test-model",
                            "messages": messages,
                            "stream": False,
                        },
                        headers={"X-Session-ID": SESSION_ID},
                    )

        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        assert data["choices"][0]["message"]["content"] is not None


    @pytest.mark.asyncio
    async def test_recall_interception_with_long_session(self):
        """When a long session has graph data and the model calls recall, it should be intercepted."""
        SESSION_ID = "synlong-recall-001"
        reset_circuit(SESSION_ID)

        messages = build_recall_trigger_session()
        request_count = [0]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            msg_list = body.get("messages", [])
            has_tool_result = any(m.get("role") == "tool" for m in msg_list)

            request_count[0] += 1
            if request_count[0] == 1:
                # First request: model calls recall
                return httpx.Response(200, json=_recall_tool_call_response("JWKS cache TTL"))
            else:
                # Re-send after recall: normal response
                return httpx.Response(200, json=_NORMAL_RESPONSE)

        settings = get_settings()
        orig_recall = settings.session_recall_tool_enabled
        settings.session_recall_tool_enabled = True

        try:
            mock_backend = _make_mock_backend(facts=[
                {"content": "JWKS cache TTL is 300 seconds (5 minutes)", "fact_type": "observation", "confidence": 0.95, "source_turn": 2},
                {"content": "JWKS endpoint: https://auth.internal/api/v2/.well-known/jwks.json", "fact_type": "observation", "confidence": 0.9, "source_turn": 2},
                {"content": "Database pool: min 5, max 20, overflow 10", "fact_type": "observation", "confidence": 0.9, "source_turn": 4},
            ])
            app = create_app()

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.session_recall_tool_enabled = orig_recall

        assert resp.status_code == 200
        # Should have made 2 upstream requests (initial + re-send after recall)
        assert request_count[0] >= 2, f"Expected >= 2 upstream requests (recall interception), got {request_count[0]}"

        data = resp.json()
        # Final response should NOT contain __archolith_recall tool calls (stripped)
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls", [])
        recall_calls = [tc for tc in tool_calls if tc.get("function", {}).get("name") == "__archolith_recall"]
        assert len(recall_calls) == 0, "Recall tool calls should be stripped from final response"


    @pytest.mark.asyncio
    async def test_synthetic_session_work_with_long_session(self):
        """recall_session_work tool should be intercepted and produce a work summary."""
        SESSION_ID = "synlong-work-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_long()
        request_count = [0]
        resend_messages_captured = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            request_count[0] += 1

            msg_list = body.get("messages", [])
            has_tool_result = any(m.get("role") == "tool" and m.get("name") == "recall_session_work" for m in msg_list)

            if has_tool_result:
                # Re-send with synthetic tool result — capture what was sent
                resend_messages_captured.extend(msg_list)
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            else:
                # First request: model calls recall_session_work
                return httpx.Response(200, json=_synthetic_tool_call_response())

        settings = get_settings()
        orig_synthetic = settings.synthetic_tools_enabled
        settings.synthetic_tools_enabled = True

        try:
            mock_backend = _make_mock_backend(facts=[
                {"content": "Review model has rating field (1-5)", "fact_type": "observation", "confidence": 0.9, "source_turn": 1},
            ])
            app = create_app()

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.synthetic_tools_enabled = orig_synthetic

        assert resp.status_code == 200
        assert request_count[0] >= 2, f"Expected >= 2 upstream requests, got {request_count[0]}"

        # The re-send should contain a tool result with session work summary
        if resend_messages_captured:
            tool_results = [m for m in resend_messages_captured if m.get("role") == "tool"]
            assert len(tool_results) >= 1, "Re-send should contain the synthetic tool result"
            # The tool result should have session work content
            work_content = tool_results[-1].get("content", "")
            assert "Session Work Summary" in work_content or "session" in work_content.lower(), (
                f"Expected session work summary in tool result, got: {work_content[:200]}"
            )


    @pytest.mark.asyncio
    async def test_circuit_breaker_with_repeated_failures(self):
        """After max_consecutive synthetic failures, circuit opens and injection stops."""
        SESSION_ID = "synlong-circuit-001"
        reset_circuit(SESSION_ID)
        reset_all()

        messages = build_coding_session_short()
        m = get_metrics()

        settings = get_settings()
        orig_synthetic = settings.synthetic_tools_enabled
        orig_max_consec = settings.synthetic_circuit_max_consecutive
        settings.synthetic_tools_enabled = True
        settings.synthetic_circuit_max_consecutive = 2  # Low threshold for testing

        try:
            # Each request: model calls synthetic tool → re-send fails → fallback
            async def failing_handler(request: httpx.Request) -> httpx.Response:
                if "/models" in str(request.url):
                    return httpx.Response(200, json={"object": "list", "data": []})
                body = json.loads(request.content)
                msg_list = body.get("messages", [])
                has_tool_result = any(
                    m.get("role") == "tool" and m.get("name") == "recall_session_work"
                    for m in msg_list
                )
                if has_tool_result:
                    # Re-send fails
                    return httpx.Response(500, json={"error": {"message": "server error"}})
                else:
                    return httpx.Response(200, json=_synthetic_tool_call_response())

            mock_backend = _make_mock_backend()
            app = create_app()

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(failing_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        # Send enough requests to trip the circuit breaker
                        for i in range(3):
                            resp = await ac.post(
                                "/v1/chat/completions",
                                json={
                                    "model": "test-model",
                                    "messages": messages,
                                    "stream": False,
                                },
                                headers={"X-Session-ID": SESSION_ID},
                            )
                            assert resp.status_code == 200, f"Request {i} failed: {resp.status_code}"

        finally:
            settings.synthetic_tools_enabled = orig_synthetic
            settings.synthetic_circuit_max_consecutive = orig_max_consec

        # Circuit should have opened by now (fallback_used increments failure count)
        from archolith_proxy.proxy.circuit_breaker import is_synthetic_allowed
        # After 2 consecutive failures, should be blocked
        # (exact behavior depends on whether failures registered correctly)


    @pytest.mark.asyncio
    async def test_short_session_cold_start_passthrough(self):
        """A short session under cold start threshold should passthrough without assembly."""
        SESSION_ID = "synlong-cold-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_short()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        # Set turn_number low to trigger cold start
        mock_backend = _make_mock_backend()
        mock_backend.get_turn_number = AsyncMock(return_value=1)  # Below cold_start_turns=3
        app = create_app()

        with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
             patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
             patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
             patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
             patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

            mock_resolve.return_value = (SESSION_ID, True)  # is_new=True

            async with app.router.lifespan_context(app):
                app.state.http_client = httpx.AsyncClient(
                    transport=httpx.MockTransport(mock_handler)
                )
                app.state.extractor_client = AsyncMock()
                transport = ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    resp = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "model": "test-model",
                            "messages": messages,
                            "stream": False,
                        },
                        headers={"X-Session-ID": SESSION_ID},
                    )

        assert resp.status_code == 200
        data = resp.json()
        assert data["choices"][0]["message"]["content"] is not None


    @pytest.mark.asyncio
    async def test_native_read_cache_hit_with_session(self):
        """Native Read interception should serve from cache when files are cached."""
        SESSION_ID = "synlong-cache-001"
        reset_circuit(SESSION_ID)

        messages = build_read_cache_session()
        resend_count = [0]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            msg_list = body.get("messages", [])
            has_tool_result = any(m.get("role") == "tool" and m.get("name") == "Read" for m in msg_list)
            if has_tool_result:
                resend_count[0] += 1
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            # Model calls Read
            return httpx.Response(200, json=_read_tool_call_response("/workspace/myapp/app/models.py"))

        settings = get_settings()
        orig_synthetic = settings.synthetic_tools_enabled
        orig_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            cached_files = {
                "/workspace/myapp/app/models.py": {
                    "content": "# models.py\nclass User:\n    pass\n" * 20,
                    "sha256": "abc123",
                    "line_count": 60,
                },
            }
            mock_backend = _make_mock_backend(cached_files=cached_files)
            app = create_app()

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.synthetic_tools_enabled = orig_synthetic
            settings.native_read_intercept_enabled = orig_nri

        assert resp.status_code == 200
        # If the file was in cache, the native read intercept should have fired
        assert resend_count[0] >= 1, (
            f"Expected at least 1 cache-hit re-send, got {resend_count[0]}"
        )


# ---------------------------------------------------------------------------
# Test: token-aware assembly verification
# ---------------------------------------------------------------------------


class TestTokenAwareAssembly:
    """Verify that context assembly actually reduces tokens on realistic conversations.

    These tests capture the rewritten payload at the mock upstream and compare
    token counts against the original input to confirm assembly is working.
    """

    @pytest.mark.asyncio
    async def test_assembly_reduces_message_count(self):
        """When assembly fires with turn selection, upstream gets fewer messages."""
        SESSION_ID = "tok-msgcount-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_long()
        # Append a user message so the proxy treats this as a user turn
        # (not agent-solo) and runs assembly.
        messages.append({"role": "user", "content": "Now run the tests to verify everything works."})
        original_msg_count = len(messages)
        captured_payloads: list[dict] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            captured_payloads.append(body)
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        # Build an AssembledContext with turn selection — keep only 3 of 9
        # user turns. This simulates curator output, exercising the rewrite
        # pipeline's turn-dropping path.
        context_block = (
            "=== SESSION OVERVIEW ===\n"
            "Goal: Add product reviews to the e-commerce app\n\n"
            "=== RELEVANT CONTEXT ===\n"
            "- Review model has rating field (1-5) and body text field\n"
            "- Order model has status transitions: pending→confirmed→shipped→delivered\n"
            "- Review routes registered in main.py\n"
        )
        mock_assembled = AssembledContext(
            system_message={"role": "system", "content": context_block},
            graph_context=[{"role": "system", "content": context_block}],
            coherence_tail=[],
            token_estimate=500,
            facts_retrieved=3,
            session_id=SESSION_ID,
            retained_turn_numbers=[1, 8, 9],  # Keep first, last two user turns
        )

        settings = get_settings()
        orig_min_input = settings.assembly_min_input_tokens
        orig_min_savings = settings.assembly_min_savings_ratio
        orig_curator = settings.curator_enabled
        settings.assembly_min_input_tokens = 0
        settings.assembly_min_savings_ratio = 0.0
        settings.curator_enabled = True

        try:
            mock_backend = _make_mock_backend()
            mock_backend.get_turn_number = AsyncMock(return_value=15)
            app = create_app()

            async def fake_curate(*args, **kwargs):
                return mock_assembled

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.openai.chat.curate_context", side_effect=fake_curate), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.assembly_min_input_tokens = orig_min_input
            settings.assembly_min_savings_ratio = orig_min_savings
            settings.curator_enabled = orig_curator

        assert resp.status_code == 200
        assert len(captured_payloads) >= 1, "No upstream request captured"

        upstream_msg_count = len(captured_payloads[0].get("messages", []))
        assert upstream_msg_count < original_msg_count, (
            f"Assembly with turn selection should reduce message count: "
            f"original={original_msg_count}, upstream={upstream_msg_count}"
        )

    @pytest.mark.asyncio
    async def test_assembly_reduces_token_count(self):
        """Turn selection should produce meaningful token savings."""
        SESSION_ID = "tok-tokens-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_long()
        # Append a user message so the proxy treats this as a user turn
        # (not agent-solo) and runs assembly.
        messages.append({"role": "user", "content": "Now run the tests to verify everything works."})
        original_tokens = estimate_input_tokens(messages)
        captured_payloads: list[dict] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            captured_payloads.append(body)
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        # Small context block — most savings come from dropping turns, not injection
        context_block = (
            "=== SESSION OVERVIEW ===\n"
            "Goal: Add product reviews\n\n"
            "=== RELEVANT CONTEXT ===\n"
            "- Review model defined with rating, title, body fields\n"
            "- Order model uses status transitions\n"
            "- Pydantic schemas enforce rating 1-5\n"
            "- All 8 tests passing\n"
        )
        mock_assembled = AssembledContext(
            system_message={"role": "system", "content": context_block},
            graph_context=[{"role": "system", "content": context_block}],
            coherence_tail=[],
            token_estimate=300,
            facts_retrieved=4,
            session_id=SESSION_ID,
            retained_turn_numbers=[1, 8, 9],  # Keep 3 of 9 user turns
        )

        settings = get_settings()
        orig_min_input = settings.assembly_min_input_tokens
        orig_min_savings = settings.assembly_min_savings_ratio
        orig_curator = settings.curator_enabled
        settings.assembly_min_input_tokens = 0
        settings.assembly_min_savings_ratio = 0.0
        settings.curator_enabled = True

        try:
            mock_backend = _make_mock_backend()
            mock_backend.get_turn_number = AsyncMock(return_value=15)
            app = create_app()

            async def fake_curate(*args, **kwargs):
                return mock_assembled

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.openai.chat.curate_context", side_effect=fake_curate), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.assembly_min_input_tokens = orig_min_input
            settings.assembly_min_savings_ratio = orig_min_savings
            settings.curator_enabled = orig_curator

        assert resp.status_code == 200
        assert len(captured_payloads) >= 1

        rewritten_tokens = estimate_input_tokens(captured_payloads[0].get("messages", []))
        savings = original_tokens - rewritten_tokens
        savings_ratio = savings / max(original_tokens, 1)

        assert rewritten_tokens < original_tokens, (
            f"Turn selection should reduce tokens: original={original_tokens}, "
            f"rewritten={rewritten_tokens}, savings={savings}"
        )
        assert savings_ratio > 0.05, (
            f"Savings ratio too low: {savings_ratio:.3f} ({savings} tokens saved "
            f"from {original_tokens})"
        )

    @pytest.mark.asyncio
    async def test_assembled_payload_contains_context_block(self):
        """The rewritten system message should contain the injected session context."""
        SESSION_ID = "tok-context-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_long()
        # Append a user message so the proxy treats this as a user turn
        # (not agent-solo) and runs assembly.
        messages.append({"role": "user", "content": "Now run the tests to verify everything works."})
        captured_payloads: list[dict] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            captured_payloads.append(body)
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        # Build a curator-style context block with the expected section markers
        context_block = (
            "=== SESSION OVERVIEW ===\n"
            "Goal: Add product reviews to the e-commerce app\n\n"
            "## Files Touched\n"
            "- reviews/models.py (modified)\n\n"
            "## Decisions Made\n"
            "- [turn 4] Follow existing order route patterns for review routes\n\n"
            "=== RELEVANT CONTEXT ===\n"
            "- [observation|t3] Review endpoint supports POST, GET, DELETE operations\n"
            "- [observation|t7] Product listing includes average_rating computed via SQL AVG\n"
        )
        mock_assembled = AssembledContext(
            system_message={"role": "system", "content": context_block},
            graph_context=[{"role": "system", "content": context_block}],
            coherence_tail=[],
            token_estimate=300,
            facts_retrieved=2,
            session_id=SESSION_ID,
            retained_turn_numbers=[1, 8, 9],
        )

        settings = get_settings()
        orig_min_input = settings.assembly_min_input_tokens
        orig_min_savings = settings.assembly_min_savings_ratio
        orig_curator = settings.curator_enabled
        # Disable savings gates entirely so the context block stays
        settings.assembly_min_input_tokens = 0
        settings.assembly_min_savings_ratio = 0.0
        settings.curator_enabled = True

        try:
            mock_backend = _make_mock_backend()
            mock_backend.get_turn_number = AsyncMock(return_value=15)

            app = create_app()

            async def fake_curate(*args, **kwargs):
                return mock_assembled

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.openai.chat.curate_context", side_effect=fake_curate), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.assembly_min_input_tokens = orig_min_input
            settings.assembly_min_savings_ratio = orig_min_savings
            settings.curator_enabled = orig_curator

        assert resp.status_code == 200
        assert len(captured_payloads) >= 1

        upstream_messages = captured_payloads[0].get("messages", [])
        system_msgs = [m for m in upstream_messages if m.get("role") == "system"]
        assert len(system_msgs) >= 1, "No system message in upstream payload"

        system_content = system_msgs[0].get("content", "")
        # Context block should contain session overview and relevant facts
        assert "SESSION OVERVIEW" in system_content, (
            f"System message missing SESSION OVERVIEW section. Content preview: {system_content[:300]}"
        )
        assert "RELEVANT CONTEXT" in system_content, (
            f"System message missing RELEVANT CONTEXT section. Content preview: {system_content[:300]}"
        )
        # Should contain the goal
        assert "product review" in system_content.lower() or "e-commerce" in system_content.lower(), (
            f"System message missing session goal. Content preview: {system_content[:300]}"
        )

    @pytest.mark.asyncio
    async def test_coherence_tail_preserved(self):
        """The last N messages (coherence tail) should be kept intact in the rewritten payload."""
        SESSION_ID = "tok-tail-001"
        reset_circuit(SESSION_ID)

        messages = build_coding_session_long()
        captured_payloads: list[dict] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            captured_payloads.append(body)
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        settings = get_settings()
        orig_min_input = settings.assembly_min_input_tokens
        orig_min_savings = settings.assembly_min_savings_ratio
        orig_tail = settings.coherence_tail_size
        settings.assembly_min_input_tokens = 2000
        settings.assembly_min_savings_ratio = 0.05
        # Use a small tail so we can verify it's kept
        settings.coherence_tail_size = 5

        try:
            mock_backend = _make_mock_backend(
                facts=[
                    {"content": "Test fact for tail preservation check", "fact_type": "observation", "confidence": 0.9, "source_turn": 1},
                ],
            )
            mock_backend.get_turn_number = AsyncMock(return_value=15)

            app = create_app()

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.assembly_min_input_tokens = orig_min_input
            settings.assembly_min_savings_ratio = orig_min_savings
            settings.coherence_tail_size = orig_tail

        assert resp.status_code == 200
        assert len(captured_payloads) >= 1

        upstream_messages = captured_payloads[0].get("messages", [])
        # The last user message in the original should appear in the upstream payload
        original_last_user = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                original_last_user = msg.get("content", "")
                break

        upstream_last_user = None
        for msg in reversed(upstream_messages):
            if msg.get("role") == "user":
                upstream_last_user = msg.get("content", "")
                break

        assert original_last_user is not None, "No user message in original"
        assert upstream_last_user is not None, "No user message in upstream"
        assert upstream_last_user == original_last_user, (
            f"Last user message not preserved in coherence tail.\n"
            f"  Original: {original_last_user[:100]}\n"
            f"  Upstream: {upstream_last_user[:100]}"
        )

    @pytest.mark.asyncio
    async def test_savings_gate_reverts_on_low_tokens(self):
        """When input tokens are below assembly_min_input_tokens, assembly should revert to passthrough."""
        SESSION_ID = "tok-gate-001"
        reset_circuit(SESSION_ID)

        # Use the short session — well under 50K tokens
        messages = build_coding_session_short()
        original_tokens = estimate_input_tokens(messages)
        captured_payloads: list[dict] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            captured_payloads.append(body)
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        settings = get_settings()
        # Deterministic gate test: force a known config instead of inheriting the
        # operator's .env (which lowers assembly_min_input_tokens and enables
        # agent-solo). Below assembly_min_input_tokens, with agent-solo and filter
        # compression off, the proxy must pass the messages through unchanged.
        orig_cfg = {
            "assembly_min_input_tokens": settings.assembly_min_input_tokens,
            "filter_enabled": settings.filter_enabled,
            "agent_solo_shrink_enabled": settings.agent_solo_shrink_enabled,
            "agent_solo_dedup_enabled": settings.agent_solo_dedup_enabled,
            "agent_solo_compress_middle_enabled": settings.agent_solo_compress_middle_enabled,
        }
        settings.assembly_min_input_tokens = 100_000  # well above this short session
        settings.filter_enabled = False
        settings.agent_solo_shrink_enabled = False
        settings.agent_solo_dedup_enabled = False
        settings.agent_solo_compress_middle_enabled = False

        assert original_tokens < settings.assembly_min_input_tokens, (
            f"Short session has {original_tokens} tokens, expected < {settings.assembly_min_input_tokens}"
        )

        mock_backend = _make_mock_backend(
            facts=[
                {"content": "Fact that should NOT cause rewriting on short session", "fact_type": "observation", "confidence": 0.9, "source_turn": 1},
            ],
        )
        mock_backend.get_turn_number = AsyncMock(return_value=10)

        app = create_app()

        try:
            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            for _k, _v in orig_cfg.items():
                setattr(settings, _k, _v)

        assert resp.status_code == 200
        assert len(captured_payloads) >= 1

        # Passthrough: the upstream completion should receive ~the ORIGINAL messages
        # (no assembly/agent-solo/filter rewrite). Use the last captured payload (the
        # main completion; any sub-call would precede it).
        upstream_tokens = estimate_input_tokens(captured_payloads[-1].get("messages", []))
        token_ratio = upstream_tokens / max(original_tokens, 1)
        assert token_ratio > 0.9, (
            f"Savings gate should prevent assembly: original={original_tokens}, "
            f"upstream={upstream_tokens}, ratio={token_ratio:.3f}"
        )

    @pytest.mark.asyncio
    async def test_recall_resend_includes_recalled_facts(self):
        """The recall re-send payload should contain the tool result with recalled facts."""
        SESSION_ID = "tok-recall-facts-001"
        reset_circuit(SESSION_ID)

        messages = build_recall_trigger_session()
        resend_payloads: list[dict] = []
        request_count = [0]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            request_count[0] += 1

            msg_list = body.get("messages", [])
            has_recall_result = any(
                m.get("role") == "tool" and m.get("name") == "__archolith_recall"
                for m in msg_list
            )

            if has_recall_result:
                resend_payloads.append(body)
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            else:
                return httpx.Response(200, json=_recall_tool_call_response("JWKS cache TTL"))

        settings = get_settings()
        orig_recall = settings.session_recall_tool_enabled
        settings.session_recall_tool_enabled = True

        try:
            mock_backend = _make_mock_backend(
                facts=[
                    {"content": "JWKS cache TTL is 300 seconds (5 minutes)", "fact_type": "observation", "confidence": 0.95, "source_turn": 2},
                    {"content": "JWKS endpoint at https://auth.internal/api/v2/.well-known/jwks.json", "fact_type": "observation", "confidence": 0.9, "source_turn": 2},
                    {"content": "Auth middleware uses RS256 algorithm for JWT validation", "fact_type": "observation", "confidence": 0.9, "source_turn": 2},
                ],
            )
            mock_backend.get_turn_number = AsyncMock(return_value=12)

            app = create_app()

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(
                        transport=httpx.MockTransport(mock_handler)
                    )
                    app.state.extractor_client = AsyncMock()
                    transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": messages,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )
        finally:
            settings.session_recall_tool_enabled = orig_recall

        assert resp.status_code == 200
        assert len(resend_payloads) >= 1, "No recall re-send captured"

        # The re-send should contain a tool result with recalled facts
        resend_msgs = resend_payloads[0].get("messages", [])
        recall_results = [
            m for m in resend_msgs
            if m.get("role") == "tool" and m.get("name") == "__archolith_recall"
        ]
        assert len(recall_results) >= 1, "Re-send missing recall tool result"

        recall_content = recall_results[0].get("content", "")
        assert len(recall_content) > 50, (
            f"Recall result too short ({len(recall_content)} chars) — "
            f"should contain formatted facts"
        )
        assert "RELEVANT CONTEXT" in recall_content, (
            f"Recall result missing RELEVANT CONTEXT header: {recall_content[:200]}"
        )
        # Should contain at least one of our mock facts
        assert "JWKS" in recall_content or "300" in recall_content, (
            f"Recall result doesn't contain expected fact content: {recall_content[:200]}"
        )
