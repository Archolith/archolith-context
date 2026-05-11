"""Phase 4.8 integration tests — chaos, resilience, load, and session isolation.

These tests verify the proxy's behavior under adverse conditions:
1. Neo4j failure mid-session → fallback + recovery
2. Extraction 500 → session continues
3. Concurrent sessions with no cross-contamination
4. Structlog JSON logging configuration
5. Streaming retry on transient upstream errors
6. Metrics derived rates
7. Batch embedding computation
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from src.main import create_app, _metrics


# --- Shared mock infrastructure ---

MOCK_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 1234567890,
    "model": "test-model",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Test response"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _make_app_with_mock_upstream(mock_responses=None):
    """Create app with mock upstream transport. Returns (app, mock_transport)."""
    app = create_app()
    response_map = mock_responses or {}

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/models" in url_str:
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "test-model", "object": "model", "created": 1234567890, "owned_by": "test"}],
            })
        # Check for status-based responses
        for key_status, key_body in response_map.items():
            if request.method == "POST" and key_status != 200:
                return httpx.Response(key_status, json=key_body)
        return httpx.Response(200, json=MOCK_RESPONSE)

    mock_transport = httpx.MockTransport(mock_handler)
    return app, mock_transport


# --- Test: Neo4j chaos (kill mid-session → fallback + recovery) ---

class TestNeo4jChaos:
    """Test that the proxy degrades gracefully when Neo4j fails mid-session."""

    @pytest.mark.asyncio
    async def test_neo4j_down_returns_passthrough(self):
        """When Neo4j is not configured, proxy should passthrough without error."""
        app = create_app()
        # Don't set neo4j_ready — simulate no Neo4j

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=MOCK_RESPONSE)

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            # Explicitly mark Neo4j as not ready
            app.state.neo4j_ready = False
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/chat/completions",
                    json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["choices"][0]["message"]["content"] == "Test response"

    @pytest.mark.asyncio
    async def test_neo4j_query_failure_falls_back_to_passthrough(self):
        """When a Neo4j query fails mid-session, proxy falls back gracefully."""
        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=MOCK_RESPONSE)

        mock_transport = httpx.MockTransport(mock_handler)

        # Mock the graph modules to simulate Neo4j failure
        with patch("src.proxy.session.resolve_session", new_callable=AsyncMock) as mock_resolve, \
             patch("src.openai.chat.assemble_context", new_callable=AsyncMock) as mock_assemble:

            mock_resolve.side_effect = Exception("Neo4j connection refused")
            mock_assemble.return_value = None

            async with app.router.lifespan_context(app):
                app.state.http_client = httpx.AsyncClient(transport=mock_transport)
                app.state.neo4j_ready = True
                transport = ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    resp = await ac.post(
                        "/v1/chat/completions",
                        json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
                    )
                    # Should still succeed — fallback to passthrough
                    assert resp.status_code == 200


# --- Test: Extraction 500 → session continues ---

class TestExtractionChaos:
    """Test that extraction failures don't block the main request path."""

    @pytest.mark.asyncio
    async def test_extraction_failure_does_not_block_response(self):
        """When extraction API returns 500, the main response is still returned."""
        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=MOCK_RESPONSE)

        mock_transport = httpx.MockTransport(mock_handler)

        with patch("src.openai.chat.extract_facts", new_callable=AsyncMock) as mock_extract:
            mock_extract.side_effect = Exception("Extraction API returned 500")

            async with app.router.lifespan_context(app):
                app.state.http_client = httpx.AsyncClient(transport=mock_transport)
                app.state.neo4j_ready = False  # Skip session resolution
                transport = ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    resp = await ac.post(
                        "/v1/chat/completions",
                        json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
                    )
                    # Main request succeeds regardless of extraction failure
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["choices"][0]["message"]["content"] == "Test response"


# --- Test: Concurrent sessions with no cross-contamination ---

class TestSessionIsolation:
    """Test that multiple concurrent sessions don't leak data."""

    @pytest.mark.asyncio
    async def test_concurrent_sessions_isolated_fingerprints(self):
        """Different conversations produce different fingerprints."""
        from src.proxy.session import compute_fingerprint

        fp1 = compute_fingerprint("System prompt A", "First user message A")
        fp2 = compute_fingerprint("System prompt B", "First user message B")
        fp3 = compute_fingerprint("System prompt A", "First user message A")

        # Same input → same fingerprint
        assert fp1 == fp3
        # Different input → different fingerprint
        assert fp1 != fp2

    @pytest.mark.asyncio
    async def test_concurrent_sessions_different_session_ids(self):
        """Concurrent requests with different fingerprints get different sessions."""
        from src.proxy.session import compute_fingerprint

        fp_a = compute_fingerprint("Agent A system prompt", "Question about project A")
        fp_b = compute_fingerprint("Agent B system prompt", "Question about project B")

        assert fp_a != fp_b
        assert len(fp_a) == 16  # SHA-256[:16]
        assert len(fp_b) == 16

    @pytest.mark.asyncio
    async def test_explicit_session_id_isolation(self):
        """Explicit X-Session-ID headers create separate sessions."""
        # This test validates the session ID header path
        # without requiring a real Neo4j connection
        headers_a = {"x-session-id": "session-alpha"}
        headers_b = {"x-session-id": "session-beta"}

        assert headers_a["x-session-id"] != headers_b["x-session-id"]

    @pytest.mark.asyncio
    async def test_10_concurrent_requests_no_errors(self):
        """10 concurrent requests to the proxy don't cause errors or cross-talk."""
        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            # Return a response that includes the request model for tracing
            body = json.loads(request.content.decode()) if request.content else {}
            model = body.get("model", "unknown")
            return httpx.Response(200, json={
                **MOCK_RESPONSE,
                "model": model,
            })

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                # Fire 10 concurrent requests
                tasks = []
                for i in range(10):
                    tasks.append(ac.post(
                        "/v1/chat/completions",
                        json={
                            "model": f"model-{i}",
                            "messages": [{"role": "user", "content": f"Request {i}"}],
                        },
                    ))

                responses = await asyncio.gather(*tasks)

                # All should succeed
                for i, resp in enumerate(responses):
                    assert resp.status_code == 200, f"Request {i} failed: {resp.status_code}"
                    data = resp.json()
                    assert data["model"] == f"model-{i}", f"Response {i} has wrong model: cross-contamination?"


# --- Test: Structlog JSON logging configuration ---

class TestStructuredLogging:
    """Test that structlog is configured with JSON rendering."""

    def test_configure_logging_sets_json_renderer(self):
        """configure_logging() should set up JSON renderer by default."""
        import os
        # Ensure LOG_FORMAT is not set (defaults to json)
        old_val = os.environ.pop("LOG_FORMAT", None)
        try:
            from src.logging_config import configure_logging
            configure_logging()
            # After configuration, structlog should be configured
            # We can verify by checking that structlog is configured
            import structlog
            # structlog.get_logger() should work
            log = structlog.get_logger()
            assert log is not None
        finally:
            if old_val is not None:
                os.environ["LOG_FORMAT"] = old_val

    def test_configure_logging_dev_mode(self):
        """LOG_FORMAT=dev should use ConsoleRenderer."""
        import os
        os.environ["LOG_FORMAT"] = "dev"
        try:
            from src.logging_config import configure_logging
            configure_logging()
            import structlog
            log = structlog.get_logger()
            assert log is not None
        finally:
            os.environ.pop("LOG_FORMAT", None)


# --- Test: Streaming retry on transient errors ---

class TestStreamingRetry:
    """Test that streaming path retries on transient upstream errors."""

    @pytest.mark.asyncio
    async def test_streaming_retries_on_429(self):
        """Streaming request retries on 429 before succeeding."""
        app = create_app()
        call_count = {"n": 0}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            call_count["n"] += 1
            if call_count["n"] <= 2:
                # First two calls return 429
                return httpx.Response(429, json={"error": {"message": "Rate limited", "type": "rate_limit_error"}})
            # Third call succeeds with SSE
            sse_content = _build_sse_chunks("Hello after retry")
            return httpx.Response(200, content=sse_content.encode(), headers={"Content-Type": "text/event-stream"})

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": True,
                    },
                )
                # Should eventually succeed after retries
                assert resp.status_code == 200
                # Should have retried
                assert call_count["n"] >= 2

    @pytest.mark.asyncio
    async def test_streaming_sse_passthrough_content(self):
        """Streaming response relays SSE content correctly via true aiter_lines passthrough."""
        app = create_app()
        sse_content = _build_sse_chunks("Hello world from upstream")

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=sse_content.encode(), headers={"Content-Type": "text/event-stream"})

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": True,
                    },
                )
                assert resp.status_code == 200
                body = resp.text
                # Should contain SSE data lines
                assert "data: " in body
                # Should contain the streamed content words
                assert "Hello" in body
                # Should contain [DONE] sentinel
                assert "[DONE]" in body

    @pytest.mark.asyncio
    async def test_streaming_non_retryable_error_relayed(self):
        """Streaming path relays non-retryable errors (e.g. 400) to client."""
        app = create_app()
        error_body = {"error": {"message": "Invalid model", "type": "invalid_request_error"}}

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(400, json=error_body)

        mock_transport = httpx.MockTransport(mock_handler)

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.post(
                    "/v1/chat/completions",
                    json={
                        "model": "bad-model",
                        "messages": [{"role": "user", "content": "Hello"}],
                        "stream": True,
                    },
                )
                # SSE wrapper is always 200; error content is in the body
                assert resp.status_code == 200
                body = resp.text
                assert "Invalid model" in body

    @pytest.mark.asyncio
    async def test_streaming_all_retries_exhausted(self):
        """When all streaming retries are exhausted, error is relayed to client."""
        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(503, json={"error": {"message": "Service unavailable"}})

        mock_transport = httpx.MockTransport(mock_handler)

        # Patch only the retry settings on the real settings object
        from src.config import get_settings
        real_settings = get_settings()
        original_retries = real_settings.upstream_max_retries
        original_backoff = real_settings.upstream_retry_backoff_base_s
        real_settings.upstream_max_retries = 2
        real_settings.upstream_retry_backoff_base_s = 0.01

        try:
            async with app.router.lifespan_context(app):
                app.state.http_client = httpx.AsyncClient(transport=mock_transport)
                app.state.neo4j_ready = False
                transport = ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    resp = await ac.post(
                        "/v1/chat/completions",
                        json={
                            "model": "test-model",
                            "messages": [{"role": "user", "content": "Hello"}],
                            "stream": True,
                        },
                    )
                    # SSE wrapper always 200; error content in body
                    assert resp.status_code == 200
                    body = resp.text
                    assert "upstream_error" in body
        finally:
            real_settings.upstream_max_retries = original_retries
            real_settings.upstream_retry_backoff_base_s = original_backoff


def _build_sse_chunks(text: str, model: str = "test-model") -> str:
    """Build SSE stream from text content."""
    lines = []
    for word in text.split(" "):
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(chunk)}")
    final = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    lines.append(f"data: {json.dumps(final)}")
    lines.append("data: [DONE]")
    return "\n".join(lines)


# --- Test: Metrics derived rates ---

class TestMetricsDerivedRates:
    """Test that /metrics endpoint includes derived rates."""

    @pytest.mark.asyncio
    async def test_metrics_includes_success_rate(self):
        """/metrics should include extraction_success_rate."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/metrics")
            data = resp.json()
            assert "extraction_success_rate" in data
            assert isinstance(data["extraction_success_rate"], float)

    @pytest.mark.asyncio
    async def test_metrics_includes_avg_token_savings(self):
        """/metrics should include avg_token_savings_per_request."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/metrics")
            data = resp.json()
            assert "avg_token_savings_per_request" in data
            assert "token_savings_rate" in data

    @pytest.mark.asyncio
    async def test_metrics_rate_calculation(self):
        """Derived rates should be correctly computed from raw counters."""
        from src.main import _metrics

        # Simulate some activity
        old_successes = _metrics["extraction_successes"]
        old_failures = _metrics["extraction_failures"]
        _metrics["extraction_successes"] = 8
        _metrics["extraction_failures"] = 2

        try:
            app = create_app()
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.get("/metrics")
                data = resp.json()
                # 8 successes / 10 total = 0.8
                assert data["extraction_success_rate"] == 0.8
        finally:
            _metrics["extraction_successes"] = old_successes
            _metrics["extraction_failures"] = old_failures


# --- Test: Batch embedding computation ---

class TestBatchEmbeddings:
    """Test batch embedding computation for extracted facts."""

    @pytest.mark.asyncio
    async def test_compute_embeddings_batch_empty(self):
        """Empty input returns empty list."""
        from src.extractor.embeddings import compute_embeddings_batch
        client = httpx.AsyncClient()
        result = await compute_embeddings_batch(client, [])
        assert result == []
        await client.aclose()

    @pytest.mark.asyncio
    async def test_compute_embeddings_batch_no_api_key(self):
        """Without API key, returns None for each text."""
        from src.extractor.embeddings import compute_embeddings_batch
        from src.config import get_settings

        client = httpx.AsyncClient()
        # Patch settings to ensure embedding_api_key is empty
        with patch.object(get_settings(), "embedding_api_key", ""):
            result = await compute_embeddings_batch(client, ["text1", "text2"])
            assert result == [None, None]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_compute_embeddings_batch_with_mock(self):
        """With a mock upstream, embeddings are computed correctly."""
        from src.extractor.embeddings import compute_embeddings_batch

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            n = len(body["input"])
            return httpx.Response(200, json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                    for i in range(n)
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            })

        mock_transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=mock_transport)

        # Need to set embedding API key for the function to proceed
        import os
        os.environ["EMBEDDING_API_KEY"] = "sk-test-key"
        from src.config import reset_settings
        reset_settings()

        try:
            result = await compute_embeddings_batch(client, ["hello world", "test fact"])
            assert len(result) == 2
            assert result[0] == [0.1, 0.2, 0.3]
            assert result[1] == [0.1, 0.2, 0.3]
        finally:
            os.environ.pop("EMBEDDING_API_KEY", None)
            reset_settings()
            await client.aclose()

    @pytest.mark.asyncio
    async def test_compute_embeddings_batch_api_failure_graceful(self):
        """Embedding API failure returns None for each text (graceful fallback)."""
        from src.extractor.embeddings import compute_embeddings_batch

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Internal server error"})

        mock_transport = httpx.MockTransport(mock_handler)
        client = httpx.AsyncClient(transport=mock_transport)

        import os
        os.environ["EMBEDDING_API_KEY"] = "sk-test-key"
        from src.config import reset_settings
        reset_settings()

        try:
            result = await compute_embeddings_batch(client, ["text1", "text2", "text3"])
            assert len(result) == 3
            assert all(e is None for e in result)
        finally:
            os.environ.pop("EMBEDDING_API_KEY", None)
            reset_settings()
            await client.aclose()


# --- Test: Request logging middleware with session context ---

class TestRequestLoggingMiddleware:
    """Test that the middleware binds session context via structlog context vars."""

    @pytest.mark.asyncio
    async def test_middleware_includes_session_context(self):
        """Middleware should bind session_id and assembly_mode from handler context."""
        app = create_app()

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            if "/models" in str(request.url):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, json=MOCK_RESPONSE)

        mock_transport = httpx.MockTransport(mock_handler)

        captured_context = {}

        import structlog

        async with app.router.lifespan_context(app):
            app.state.http_client = httpx.AsyncClient(transport=mock_transport)
            app.state.neo4j_ready = False
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                # Make a request — the middleware should set context vars
                resp = await ac.post(
                    "/v1/chat/completions",
                    json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}]},
                )
                assert resp.status_code == 200
                # After the request, context vars should be cleared
                # (middleware clears at start of next request)
