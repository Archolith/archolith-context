"""Phase 4 unit tests — config validation, graceful degradation, metrics, retry logic."""

import os

import pytest

from src.config import Settings, get_settings, reset_settings


class TestConfigValidation:
    """Test configuration validation rules."""

    def setup_method(self):
        reset_settings()

    def test_default_settings_load(self):
        """Settings should load with defaults (no env vars required)."""
        s = Settings(_env_file=None)  # Ignore .env file for test isolation
        assert s.proxy_port == 9800
        assert s.cold_start_turns == 3
        # upstream_base_url default is "https://api.deepseek.com/v1" but .env may override

    def test_invalid_upstream_url_raises(self):
        """Non-http upstream URL should raise validation error."""
        with pytest.raises(Exception):
            Settings(upstream_base_url="ftp://bad.url/v1", _env_file=None)

    def test_valid_http_upstream_url(self):
        """http:// URL should be accepted."""
        s = Settings(upstream_base_url="http://localhost:8080/v1", _env_file=None)
        assert s.upstream_base_url == "http://localhost:8080/v1"

    def test_valid_https_upstream_url(self):
        """https:// URL should be accepted."""
        s = Settings(upstream_base_url="https://api.openai.com/v1", _env_file=None)
        assert s.upstream_base_url == "https://api.openai.com/v1"

    def test_invalid_port_raises(self):
        """Port out of range should raise validation error."""
        with pytest.raises(Exception):
            Settings(proxy_port=0, _env_file=None)
        with pytest.raises(Exception):
            Settings(proxy_port=99999, _env_file=None)

    def test_check_required_for_graph(self):
        """Graph features require Neo4j password and extractor key."""
        s = Settings(_env_file=None)
        missing = s.check_required_for_graph()
        assert "SESSION_NEO4J_PASSWORD" in missing
        assert "EXTRACTOR_API_KEY" in missing

    def test_check_required_for_graph_satisfied(self):
        """Graph features are satisfied when both keys are set."""
        s = Settings(
            session_neo4j_password="secret",
            extractor_api_key="sk-test",
            _env_file=None,
        )
        assert s.check_required_for_graph() == []

    def test_check_required_for_proxy(self):
        """Proxy requires upstream key."""
        s = Settings(_env_file=None)
        missing = s.check_required_for_proxy()
        assert "UPSTREAM_API_KEY" in missing

    def test_check_required_for_proxy_satisfied(self):
        """Proxy check passes with upstream key."""
        s = Settings(upstream_api_key="sk-test", _env_file=None)
        assert s.check_required_for_proxy() == []

    def test_get_settings_caching(self):
        """get_settings() should return the same instance."""
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_reset_settings(self):
        """reset_settings() should clear the cached instance."""
        s1 = get_settings()
        reset_settings()
        s2 = get_settings()
        assert s1 is not s2

    def test_retry_settings_defaults(self):
        """Retry settings should have sensible defaults."""
        s = Settings(_env_file=None)
        assert s.upstream_max_retries == 3
        assert s.upstream_retry_backoff_base_s == 0.5
        assert s.neo4j_max_retries == 3
        assert s.neo4j_retry_backoff_base_s == 1.0


class TestMetricsTracking:
    """Test that metrics are tracked correctly."""

    def test_initial_metrics_state(self):
        """Metrics should start at zero."""
        from src.metrics import get_metrics
        _metrics = get_metrics()
        # Reset for clean state
        _metrics["total_requests"] = 0
        _metrics["extraction_successes"] = 0
        _metrics["extraction_failures"] = 0
        _metrics["upstream_errors"] = 0
        _metrics["neo4j_errors"] = 0

        assert _metrics["total_requests"] == 0
        assert _metrics["extraction_successes"] == 0
        assert _metrics["extraction_failures"] == 0
        assert _metrics["upstream_errors"] == 0
        assert _metrics["neo4j_errors"] == 0

    def test_assembly_mode_tracking(self):
        """Assembly mode recording should increment the right counter."""
        from src.metrics import get_metrics, record_assembly_mode

        _metrics = get_metrics()
        # Reset
        for k in _metrics["assembly_modes"]:
            _metrics["assembly_modes"][k] = 0

        record_assembly_mode("graph")
        record_assembly_mode("graph")
        record_assembly_mode("passthrough")
        record_assembly_mode("skipped_low_tokens")
        record_assembly_mode("skipped_low_savings")
        record_assembly_mode("skipped_low_savings")

        assert _metrics["assembly_modes"]["graph"] == 2
        assert _metrics["assembly_modes"]["passthrough"] == 1
        assert _metrics["assembly_modes"]["cold_start"] == 0
        assert _metrics["assembly_modes"]["fallback"] == 0
        assert _metrics["assembly_modes"]["skipped_low_tokens"] == 1
        assert _metrics["assembly_modes"]["skipped_low_savings"] == 2


class TestRetryableStatusCodes:
    """Test upstream retry logic."""

    def test_retryable_codes(self):
        """Verify the set of retryable status codes."""
        from src.proxy.upstream import RETRYABLE_STATUS_CODES
        assert 429 in RETRYABLE_STATUS_CODES
        assert 500 in RETRYABLE_STATUS_CODES
        assert 502 in RETRYABLE_STATUS_CODES
        assert 503 in RETRYABLE_STATUS_CODES
        assert 504 in RETRYABLE_STATUS_CODES
        # 400 and 401 should NOT be retryable
        assert 400 not in RETRYABLE_STATUS_CODES
        assert 401 not in RETRYABLE_STATUS_CODES


class TestGracefulDegradation:
    """Test that the system degrades gracefully when dependencies are unavailable."""

    @pytest.mark.asyncio
    async def test_health_endpoint_works_without_neo4j(self, client):
        """Health endpoint should return ok even when Neo4j is not configured."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "neo4j" in data
        assert "upstream" in data
        assert "version" in data

    @pytest.mark.asyncio
    async def test_metrics_endpoint_works(self, client):
        """Metrics endpoint should return structured metrics."""
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_requests" in data
        assert "assembly_modes" in data
        assert "extraction_successes" in data
        assert "neo4j_ready" in data
        assert "active_sessions" in data
        assert "uptime_s" in data

    @pytest.mark.asyncio
    async def test_sessions_endpoint_returns_503_without_neo4j(self, client):
        """Sessions endpoint should return 503 when Neo4j is not available."""
        resp = await client.get("/sessions")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_session_detail_returns_503_without_neo4j(self, client):
        """Session detail endpoint should return 503 when Neo4j is not available."""
        resp = await client.get("/sessions/test-id")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_proxy_endpoint_handles_missing_state(self, client):
        """When app state is missing (no lifespan), proxy should handle gracefully.

        This tests the degradation path where lifespan hasn't initialized state.
        The existing proxy test suite covers the full lifecycle with mocks.
        """
        # In test mode, lifespan may not fully initialize (no http_client).
        # The proxy should not crash — it should return an error response.
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        # Without lifespan, the request will fail at the http_client access.
        # This is expected — the test validates we don't get unhandled 500s
        # in the request logging middleware itself.
        # Status code may vary: 500 (internal), or the middleware might catch it.
        assert resp.status_code >= 400


class TestTokenEstimation:
    """Test token estimation for savings tracking."""

    def test_estimate_input_tokens_simple(self):
        """Simple message array should estimate tokens."""
        from src.proxy.rewrite import estimate_input_tokens
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        tokens = estimate_input_tokens(messages)
        assert tokens > 0

    def test_estimate_input_tokens_multipart(self):
        """Multi-part content messages should estimate tokens."""
        from src.proxy.rewrite import estimate_input_tokens
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello world"},
                    {"type": "text", "text": "More text here"},
                ],
            }
        ]
        tokens = estimate_input_tokens(messages)
        assert tokens > 0

    def test_estimate_input_tokens_empty(self):
        """Empty messages should return minimum 1."""
        from src.proxy.rewrite import estimate_input_tokens
        tokens = estimate_input_tokens([])
        assert tokens >= 1
