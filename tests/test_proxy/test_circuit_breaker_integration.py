"""Integration tests for the synthetic-tools circuit breaker.

Verifies the full path through the proxy:
  - synthetic tool call triggers re-send
  - upstream 500 on re-send → fallback used → failure recorded
  - after max_consecutive failures, circuit opens
  - subsequent requests skip synthetic injection (synthetic_injections_skipped metric)
  - after cooldown, circuit recovers and injection resumes

All tests use a mock upstream and mocked graph backend — no live proxy needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.config import get_settings
from archolith_proxy.main import create_app
from archolith_proxy.metrics import get_metrics
from archolith_proxy.proxy.circuit_breaker import (
    get_circuit_state,
    is_synthetic_allowed,
    reset_all,
    reset_circuit,
)


# ---------------------------------------------------------------------------
# Fixtures / shared responses
# ---------------------------------------------------------------------------

# Upstream returns this on the initial request when synthetic tools are injected:
# the model calls recall_session_work (a synthetic tool)
_SYNTHETIC_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-syn001",
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
                "function": {"name": "recall_session_work", "arguments": "{}"},
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# Upstream returns this for normal (non-synthetic) responses
_NORMAL_RESPONSE = {
    "id": "chatcmpl-norm001",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "All done."},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _make_mock_backend():
    backend = AsyncMock()
    backend.get_turn_number = AsyncMock(return_value=1)
    backend.find_session_by_id = AsyncMock(return_value=None)
    backend.create_session = AsyncMock()
    backend.touch_session = AsyncMock()
    return backend


def _make_app_with_handler(handler):
    """Create a proxy app with a mock upstream transport."""
    app = create_app()
    return app, httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCircuitBreakerIntegration:

    @pytest.mark.asyncio
    async def test_circuit_opens_after_three_consecutive_resend_failures(self):
        """Three upstream 500s on synthetic re-send open the circuit for the session.

        Flow per request:
          Initial: proxy injects synthetic tools → upstream returns recall_session_work call
          Re-send: proxy strips synthetic, appends tool result → upstream returns 500
          → fallback_used=True → record_synthetic_failure()

        After 3 iterations: circuit state shows disabled_until > 0 and
        synthetic_circuit_opens metric incremented by 1.
        """
        SESSION_ID = "cb-test-open-001"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        opens_before = m["synthetic_circuit_opens"]
        failures_before = m["synthetic_tool_failures"]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            # Re-send: messages contain a tool-result entry for the synthetic call
            is_resend = any(m.get("role") == "tool" for m in messages)
            if is_resend:
                return httpx.Response(500, json={"error": {"message": "upstream error", "type": "server_error"}})
            # Initial request: synthetic tools injected → return synthetic call
            return httpx.Response(200, json=_SYNTHETIC_TOOL_CALL_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        settings.synthetic_tools_enabled = True

        try:
            mock_backend = _make_mock_backend()
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(transport=transport)
                    app.state.extractor_client = AsyncMock()
                    asgi_transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=asgi_transport, base_url="http://test") as ac:
                        # Three requests — each triggers a failed re-send
                        for _ in range(3):
                            await ac.post(
                                "/v1/chat/completions",
                                json={
                                    "model": "test-model",
                                    "messages": [{"role": "user", "content": "do work"}],
                                    "stream": True,
                                },
                                headers={"X-Session-ID": SESSION_ID},
                            )

        finally:
            settings.synthetic_tools_enabled = original_synthetic

        # Circuit must be open (disabled_until set)
        state = get_circuit_state(SESSION_ID)
        assert state.consecutive_failures >= 3, (
            f"Expected >= 3 consecutive failures, got {state.consecutive_failures}"
        )
        assert state.disabled_until > 0, "Circuit should be open (disabled_until > 0)"
        assert not state.hard_disabled, "Should not be hard-disabled yet (< max_total)"

        # synthetic_circuit_opens metric incremented by exactly 1
        assert m["synthetic_circuit_opens"] == opens_before + 1, (
            f"Expected synthetic_circuit_opens to increment by 1"
        )
        # synthetic_tool_failures incremented by 3
        assert m["synthetic_tool_failures"] == failures_before + 3, (
            f"Expected 3 tool failures, got {m['synthetic_tool_failures'] - failures_before}"
        )

    @pytest.mark.asyncio
    async def test_open_circuit_skips_injection_and_records_metric(self):
        """When circuit is open, synthetic tools are NOT injected and metric increments.

        Sets up an already-open circuit, then makes a streaming request. The proxy
        should skip injection (is_synthetic_allowed returns False), increment
        synthetic_injections_skipped, and forward the request normally.
        """
        import time
        from archolith_proxy.proxy.circuit_breaker import get_circuit_state

        SESSION_ID = "cb-test-skip-002"
        reset_circuit(SESSION_ID)

        # Pre-open the circuit manually
        state = get_circuit_state(SESSION_ID)
        state.consecutive_failures = 3
        state.disabled_until = time.monotonic() + 300.0  # 5 min cooldown

        m = get_metrics()
        skipped_before = m["synthetic_injections_skipped"]

        upstream_received_tools: list[list] = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            upstream_received_tools.append(body.get("tools", []))
            # Normal SSE response (circuit open → no synthetic path, so normal streaming)
            sse = "\n".join([
                f'data: {json.dumps({"id":"chatcmpl-x","object":"chat.completion.chunk","created":1234567890,"model":"test-model","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":None}]})}',
                f'data: {json.dumps({"id":"chatcmpl-x","object":"chat.completion.chunk","created":1234567890,"model":"test-model","choices":[{"index":0,"delta":{"content":"done"},"finish_reason":None}]})}',
                f'data: {json.dumps({"id":"chatcmpl-x","object":"chat.completion.chunk","created":1234567890,"model":"test-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})}',
                "data: [DONE]",
            ])
            return httpx.Response(200, content=sse.encode(), headers={"Content-Type": "text/event-stream"})

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        settings.synthetic_tools_enabled = True

        try:
            mock_backend = _make_mock_backend()
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(transport=transport)
                    app.state.extractor_client = AsyncMock()
                    asgi_transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=asgi_transport, base_url="http://test") as ac:
                        resp = await ac.post(
                            "/v1/chat/completions",
                            json={
                                "model": "test-model",
                                "messages": [{"role": "user", "content": "do more work"}],
                                "stream": True,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic

        assert resp.status_code == 200

        # synthetic_injections_skipped metric incremented
        assert m["synthetic_injections_skipped"] == skipped_before + 1, (
            f"Expected synthetic_injections_skipped to increment by 1"
        )

        # Upstream must NOT have received synthetic tool definitions
        assert upstream_received_tools, "Expected upstream to be called"
        synthetic_names = {"recall_session_work", "recall_files_read", "recall_file"}
        for tools in upstream_received_tools:
            injected_names = {t.get("function", {}).get("name") for t in tools}
            assert not injected_names & synthetic_names, (
                f"Synthetic tools should not be injected when circuit is open, got {injected_names}"
            )

    @pytest.mark.asyncio
    async def test_hard_disable_after_max_total_failures(self):
        """After max_total failures (default 10), circuit hard-disables for session lifetime.

        Uses a small max_total override to keep the test fast.
        """
        SESSION_ID = "cb-test-hard-003"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        hard_disables_before = m["synthetic_circuit_hard_disables"]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            is_resend = any(msg.get("role") == "tool" for msg in body.get("messages", []))
            if is_resend:
                return httpx.Response(500, json={"error": {"message": "error", "type": "server_error"}})
            return httpx.Response(200, json=_SYNTHETIC_TOOL_CALL_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        # Use max_total=3 so hard disable triggers quickly
        original_max_total = settings.synthetic_circuit_max_total
        original_max_consecutive = settings.synthetic_circuit_max_consecutive
        settings.synthetic_tools_enabled = True
        settings.synthetic_circuit_max_total = 3
        settings.synthetic_circuit_max_consecutive = 99  # don't trip cooldown first

        try:
            mock_backend = _make_mock_backend()
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend):

                mock_resolve.return_value = (SESSION_ID, False)

                async with app.router.lifespan_context(app):
                    app.state.http_client = httpx.AsyncClient(transport=transport)
                    app.state.extractor_client = AsyncMock()
                    asgi_transport = ASGITransport(app=app)
                    async with httpx.AsyncClient(transport=asgi_transport, base_url="http://test") as ac:
                        # 3 requests → 3 total failures → hard disable
                        for _ in range(3):
                            await ac.post(
                                "/v1/chat/completions",
                                json={
                                    "model": "test-model",
                                    "messages": [{"role": "user", "content": "work"}],
                                    "stream": True,
                                },
                                headers={"X-Session-ID": SESSION_ID},
                            )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.synthetic_circuit_max_total = original_max_total
            settings.synthetic_circuit_max_consecutive = original_max_consecutive

        state = get_circuit_state(SESSION_ID)
        assert state.hard_disabled, "Session should be hard-disabled after max_total failures"
        assert state.total_failures >= 3

        assert m["synthetic_circuit_hard_disables"] == hard_disables_before + 1, (
            "synthetic_circuit_hard_disables metric should increment by 1"
        )

        # is_synthetic_allowed must return False for a hard-disabled session
        assert not is_synthetic_allowed(SESSION_ID), (
            "is_synthetic_allowed must return False after hard disable"
        )
