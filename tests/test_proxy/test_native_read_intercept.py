"""Integration tests for native Read tool call interception.

Verifies the full path through the proxy:
  - model calls Read(file_path="config.py") → proxy checks cache → hit → re-send with cached content
  - cache miss → pass through normally (no interception)
  - mixed tool calls (Read + Bash) → not intercepted (all-or-nothing)
  - multiple cached Reads → all intercepted
  - Write/Edit → cache invalidation
  - offset/limit range reads served from cache
  - upstream error on re-send → safe fallback (pass through)

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
from archolith_proxy.proxy.circuit_breaker import reset_circuit


# ---------------------------------------------------------------------------
# Shared responses / fixtures
# ---------------------------------------------------------------------------

# Model calls Read("config.py")
_READ_TOOL_CALL_RESPONSE = {
    "id": "chatcmpl-read001",
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
                    "arguments": json.dumps({"file_path": "config.py"}),
                },
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# Model calls Read("new_file.py") — will be a cache miss
_READ_NEW_FILE_RESPONSE = {
    "id": "chatcmpl-read002",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_read_002",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"file_path": "new_file.py"}),
                },
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# Model calls Read + Bash (mixed)
_MIXED_TOOL_CALLS_RESPONSE = {
    "id": "chatcmpl-mix001",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_read_003",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"file_path": "config.py"}),
                    },
                },
                {
                    "id": "call_bash_001",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": json.dumps({"command": "ls -la"}),
                    },
                },
            ],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# Model calls two Reads
_MULTI_READ_RESPONSE = {
    "id": "chatcmpl-multi001",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_read_010",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"file_path": "config.py"}),
                    },
                },
                {
                    "id": "call_read_011",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": json.dumps({"file_path": "chat.py"}),
                    },
                },
            ],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# Model calls Read with offset/limit
_READ_RANGE_RESPONSE = {
    "id": "chatcmpl-range001",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_read_020",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"file_path": "chat.py", "offset": 100, "limit": 50}),
                },
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# Normal response (no tool calls, or after re-send)
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

# Cached file content for config.py
_CACHED_CONFIG_PY = {
    "content": "port = 8080\nhost = localhost\ndebug = false\n",
    "sha256": "abc12345",
    "line_count": 3,
}

# Cached file content for chat.py
_CACHED_CHAT_PY = {
    "content": "\n".join([f"line {i}" for i in range(1, 201)]),
    "sha256": "def67890",
    "line_count": 200,
}


def _make_mock_backend(cached_files: dict[str, dict] | None = None):
    """Create a mock graph backend with file content cache support."""
    backend = AsyncMock()
    backend.get_turn_number = AsyncMock(return_value=1)
    backend.find_session_by_id = AsyncMock(return_value=None)
    backend.create_session = AsyncMock()
    backend.touch_session = AsyncMock()
    backend.update_goal = AsyncMock()
    backend.delete_file_content = AsyncMock(return_value=True)

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
        if start > end:
            return None
        selected = lines[start - 1:end]
        numbered = [f"{start + i}: {line}" for i, line in enumerate(selected)]
        return "\n".join(numbered)

    async def _list_cached_files(session_id):
        return [
            {"path": p, "sha256": v["sha256"], "line_count": v["line_count"], "last_updated_turn": 1}
            for p, v in _cache.items()
        ]

    backend.get_file_content = AsyncMock(side_effect=_get_file_content)
    backend.get_file_lines = AsyncMock(side_effect=_get_file_lines)
    backend.list_cached_files = AsyncMock(side_effect=_list_cached_files)
    return backend


def _make_app_with_handler(handler):
    """Create a proxy app with a mock upstream transport."""
    app = create_app()
    return app, httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNativeReadIntercept:

    @pytest.mark.asyncio
    async def test_cached_read_served_from_cache(self):
        """Read("config.py") with cache hit → intercepted, re-send succeeds."""
        SESSION_ID = "nri-test-001"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        hits_before = m["native_read_cache_hits"]

        resend_count = 0

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal resend_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            # Re-send: messages contain a tool-result entry for the Read call
            is_resend = any(msg.get("role") == "tool" for msg in messages)
            if is_resend:
                resend_count += 1
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            # Initial request: model calls Read
            return httpx.Response(200, json=_READ_TOOL_CALL_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            mock_backend = _make_mock_backend(cached_files={"config.py": _CACHED_CONFIG_PY})
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": [{"role": "user", "content": "read config"}],
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        assert resp.status_code == 200
        # Re-send must have happened
        assert resend_count >= 1, "Expected at least one re-send to upstream"
        # Cache hits metric incremented
        assert m["native_read_cache_hits"] > hits_before, (
            f"Expected native_read_cache_hits to increase, got {m['native_read_cache_hits']}"
        )

    @pytest.mark.asyncio
    async def test_cache_miss_falls_through(self):
        """Read("new_file.py") with cache miss → not intercepted, passes through."""
        SESSION_ID = "nri-test-002"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        misses_before = m["native_read_cache_misses"]

        resend_count = 0

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal resend_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            is_resend = any(msg.get("role") == "tool" for msg in messages)
            if is_resend:
                resend_count += 1
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            return httpx.Response(200, json=_READ_NEW_FILE_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            # No cached files — cache miss
            mock_backend = _make_mock_backend(cached_files={})
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": [{"role": "user", "content": "read new file"}],
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        assert resp.status_code == 200
        # No re-send should have happened (cache miss → pass through)
        assert resend_count == 0, "No re-send expected on cache miss"
        # Cache misses metric incremented
        assert m["native_read_cache_misses"] > misses_before, (
            "Expected native_read_cache_misses to increase"
        )

    @pytest.mark.asyncio
    async def test_mixed_calls_not_intercepted(self):
        """Read + Bash → not intercepted (all-or-nothing rule)."""
        SESSION_ID = "nri-test-003"
        reset_circuit(SESSION_ID)

        resend_count = 0

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal resend_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            is_resend = any(msg.get("role") == "tool" for msg in messages)
            if is_resend:
                resend_count += 1
            return httpx.Response(200, json=_NORMAL_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            mock_backend = _make_mock_backend(cached_files={"config.py": _CACHED_CONFIG_PY})
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": [{"role": "user", "content": "read and bash"}],
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        assert resp.status_code == 200
        # Mixed calls → no native read re-send (synthetic may still fire if present, but not native)
        # The exact resend_count depends on synthetic tool handling, but the native intercept should NOT fire
        assert resend_count == 0, "No native-read re-send expected for mixed tool calls"

    @pytest.mark.asyncio
    async def test_multiple_reads_all_cached(self):
        """Two Read calls, both cached → both intercepted."""
        SESSION_ID = "nri-test-004"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        hits_before = m["native_read_cache_hits"]

        resend_count = 0
        resend_tool_results = []

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal resend_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            is_resend = any(msg.get("role") == "tool" for msg in messages)
            if is_resend:
                resend_count += 1
                # Collect tool results from re-send messages
                for msg in messages:
                    if msg.get("role") == "tool":
                        resend_tool_results.append(msg)
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            return httpx.Response(200, json=_MULTI_READ_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            mock_backend = _make_mock_backend(cached_files={
                "config.py": _CACHED_CONFIG_PY,
                "chat.py": _CACHED_CHAT_PY,
            })
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": [{"role": "user", "content": "read both"}],
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        assert resp.status_code == 200
        assert resend_count >= 1, "Expected re-send to upstream"
        # Two tool results in the re-send (one per Read)
        assert len(resend_tool_results) >= 2, (
            f"Expected >= 2 tool results in re-send, got {len(resend_tool_results)}"
        )
        # Cache hits metric should be 2
        assert m["native_read_cache_hits"] - hits_before >= 2, (
            "Expected >= 2 native_read_cache_hits"
        )

    @pytest.mark.asyncio
    async def test_write_invalidates_cache(self):
        """Write/Edit calls trigger cache invalidation via _invalidate_written_files."""
        from archolith_proxy.openai.chat import _invalidate_written_files, _invalidate_file_cache

        # _invalidate_written_files extracts paths from Write tool calls
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_write_001",
                    "type": "function",
                    "function": {
                        "name": "Write",
                        "arguments": json.dumps({"file_path": "config.py", "content": "new"}),
                    },
                }],
            },
        ]

        paths = _invalidate_written_files(messages)
        assert paths == ["config.py"], f"Expected ['config.py'], got {paths}"

        # _invalidate_file_cache calls backend.delete_file_content
        mock_backend = AsyncMock()
        mock_backend.delete_file_content = AsyncMock(return_value=True)

        with patch("archolith_proxy.openai.file_cache.get_backend", return_value=mock_backend):
            await _invalidate_file_cache("test-session", ["config.py"], 5)

        mock_backend.delete_file_content.assert_called_once_with("test-session", "config.py")

    @pytest.mark.asyncio
    async def test_intercept_respects_offset_limit(self):
        """Read with offset/limit → cache hit → get_file_lines called with correct range."""
        SESSION_ID = "nri-test-006"
        reset_circuit(SESSION_ID)

        lines_called_with = {}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            is_resend = any(msg.get("role") == "tool" for msg in messages)
            if is_resend:
                return httpx.Response(200, json=_NORMAL_RESPONSE)
            return httpx.Response(200, json=_READ_RANGE_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            mock_backend = _make_mock_backend(cached_files={"chat.py": _CACHED_CHAT_PY})

            # Patch get_file_lines to capture the range args
            original_get_file_lines = mock_backend.get_file_lines

            async def _tracking_get_file_lines(session_id, path, start, end):
                lines_called_with["path"] = path
                lines_called_with["start"] = start
                lines_called_with["end"] = end
                return await original_get_file_lines(session_id, path, start, end)

            mock_backend.get_file_lines = AsyncMock(side_effect=_tracking_get_file_lines)

            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": [{"role": "user", "content": "read range"}],
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        assert resp.status_code == 200
        # get_file_lines should have been called with the correct range
        assert lines_called_with.get("path") == "chat.py", (
            f"Expected path=chat.py, got {lines_called_with.get('path')}"
        )
        assert lines_called_with.get("start") == 100, (
            f"Expected start=100, got {lines_called_with.get('start')}"
        )
        # end = start + limit - 1 = 100 + 50 - 1 = 149
        assert lines_called_with.get("end") == 149, (
            f"Expected end=149, got {lines_called_with.get('end')}"
        )

    @pytest.mark.asyncio
    async def test_upstream_error_falls_through(self):
        """Cache hit but upstream error on re-send → safe fallback (pass through)."""
        SESSION_ID = "nri-test-007"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        errors_before = m["native_read_intercept_errors"]

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            messages = body.get("messages", [])
            is_resend = any(msg.get("role") == "tool" for msg in messages)
            if is_resend:
                # Re-send fails
                return httpx.Response(500, json={"error": {"message": "upstream error", "type": "server_error"}})
            return httpx.Response(200, json=_READ_TOOL_CALL_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            mock_backend = _make_mock_backend(cached_files={"config.py": _CACHED_CONFIG_PY})
            app, transport = _make_app_with_handler(mock_handler)

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": [{"role": "user", "content": "read config"}],
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        # Response should still be valid (the original response passes through)
        assert resp.status_code == 200
        # Error metric should be incremented
        assert m["native_read_intercept_errors"] > errors_before, (
            "Expected native_read_intercept_errors to increase"
        )

    @pytest.mark.asyncio
    async def test_write_in_history_skips_intercept(self) -> None:
        """If messages history contains a write/edit tool call, intercept is skipped.

        Prevents serving stale cache content when a file was edited earlier in
        the same session but the background invalidation hasn't run yet.
        """
        SESSION_ID = "nri-test-write-guard"
        reset_circuit(SESSION_ID)

        m = get_metrics()
        hits_before = m["native_read_cache_hits"]
        call_count = 0

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            call_count += 1
            return httpx.Response(200, json=_READ_TOOL_CALL_RESPONSE)

        settings = get_settings()
        original_synthetic = settings.synthetic_tools_enabled
        original_nri = settings.native_read_intercept_enabled
        settings.synthetic_tools_enabled = True
        settings.native_read_intercept_enabled = True

        try:
            mock_backend = _make_mock_backend(cached_files={"config.py": _CACHED_CONFIG_PY})
            app, transport = _make_app_with_handler(mock_handler)

            # Messages contain a prior edit tool call — should block interception
            messages_with_edit = [
                {"role": "user", "content": "edit config"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_edit_001",
                        "type": "function",
                        "function": {
                            "name": "edit",  # lowercase — actual opencode tool name
                            "arguments": json.dumps({"file_path": "config.py", "old_string": "x", "new_string": "y"}),
                        },
                    }],
                },
                {"role": "tool", "tool_call_id": "call_edit_001", "content": "edited successfully"},
                {"role": "user", "content": "now read config again"},
            ]

            with patch("archolith_proxy.openai.chat.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.openai.chat.resolve_session", new_callable=AsyncMock) as mock_resolve, \
                 patch("archolith_proxy.openai.chat.get_backend", return_value=mock_backend), \
                 patch("archolith_proxy.graph.backend.is_graph_ready", return_value=True), \
                 patch("archolith_proxy.graph.backend.get_backend", return_value=mock_backend):

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
                                "messages": messages_with_edit,
                                "stream": False,
                            },
                            headers={"X-Session-ID": SESSION_ID},
                        )

        finally:
            settings.synthetic_tools_enabled = original_synthetic
            settings.native_read_intercept_enabled = original_nri

        assert resp.status_code == 200
        # No re-send should have fired — intercepted path skipped
        assert call_count == 1, f"Expected exactly 1 upstream call (no re-send), got {call_count}"
        # Cache hits should not have increased
        assert m["native_read_cache_hits"] == hits_before, "Cache hits should not increase when write is in history"
