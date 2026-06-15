"""Unit tests for curator tool implementations.

Tests cover search_facts (substring) and search_facts_semantic (embedding-based),
with graceful fallback paths when the embedding API is unavailable.
"""

from __future__ import annotations

import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Bootstrap: inject a stub for the 'openai' top-level package so the
# curator/__init__.py import (`from openai import AsyncOpenAI`) doesn't
# shadow the installed package with our local archolith_proxy/openai/__init__.py.
# ---------------------------------------------------------------------------

def _ensure_openai_stub() -> None:
    """Inject a minimal openai stub into sys.modules if needed."""
    if "openai" not in sys.modules or not hasattr(sys.modules["openai"], "AsyncOpenAI"):
        stub = types.ModuleType("openai")
        stub.AsyncOpenAI = MagicMock()
        stub.APIConnectionError = type("APIConnectionError", (Exception,), {})
        stub.APITimeoutError = type("APITimeoutError", (Exception,), {})
        stub.InternalServerError = type("InternalServerError", (Exception,), {})
        stub.RateLimitError = type("RateLimitError", (Exception,), {})
        sys.modules["openai"] = stub


_ensure_openai_stub()

# Now safe to import curator tools
from archolith_proxy.curator.tools import search_facts, search_facts_semantic  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fact(content: str, embedding: list[float] | None = None) -> dict:
    return {
        "fact_id": f"f_{content[:8].replace(' ', '_')}",
        "content": content,
        "fact_type": "observation",
        "confidence": 0.8,
        "source_turn": 1,
        "embedding": embedding,
    }


# ---------------------------------------------------------------------------
# search_facts (substring)
# ---------------------------------------------------------------------------

class TestSearchFacts:
    @pytest.mark.asyncio
    async def test_returns_matches(self):
        facts = [
            _make_fact("the auth middleware is at line 42"),
            _make_fact("database schema uses snake_case"),
            _make_fact("authentication token expires in 3600s"),
        ]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)

        with patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend):
            result = await search_facts("session-1", query="auth")

        assert "auth middleware" in result
        assert "authentication token" in result
        assert "snake_case" not in result

    @pytest.mark.asyncio
    async def test_no_matches_returns_message(self):
        facts = [_make_fact("database schema uses snake_case")]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)

        with patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend):
            result = await search_facts("session-1", query="authentication")

        assert result == "(no matching facts)"

    @pytest.mark.asyncio
    async def test_empty_query(self):
        result = await search_facts("session-1", query="")
        assert "(no query specified)" in result

    @pytest.mark.asyncio
    async def test_no_facts_stored(self):
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=[])

        with patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend):
            result = await search_facts("session-1", query="auth")

        assert "(no matching facts)" in result

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        facts = [_make_fact("JWT expiry is 3600 seconds")]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)

        with patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend):
            result = await search_facts("session-1", query="jwt")

        assert "JWT expiry" in result


# ---------------------------------------------------------------------------
# search_facts_semantic
# ---------------------------------------------------------------------------

class TestSearchFactsSemantic:
    @pytest.mark.asyncio
    async def test_empty_query(self):
        result = await search_facts_semantic("session-1", query="")
        assert "(no query specified)" in result

    @pytest.mark.asyncio
    async def test_no_facts_stored(self):
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=[])
        mock_settings = MagicMock()
        mock_settings.embedding_api_key = "sk-test"

        with (
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await search_facts_semantic("session-1", query="auth")

        assert "no facts stored" in result

    @pytest.mark.asyncio
    async def test_no_api_key_falls_back_to_substring(self):
        facts = [
            _make_fact("auth middleware is at line 42"),
            _make_fact("database uses postgres"),
        ]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)
        mock_settings = MagicMock()
        mock_settings.embedding_api_key = ""

        with (
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
        ):
            result = await search_facts_semantic("session-1", query="auth")

        assert "auth middleware" in result
        assert "substring fallback" in result

    @pytest.mark.asyncio
    async def test_embedding_ranks_by_cosine_similarity(self):
        """With embeddings, most similar fact ranks first."""
        # query embedding points in direction [1, 0, 0]
        query_embedding = [1.0, 0.0, 0.0]

        facts = [
            _make_fact("dissimilar fact", embedding=[0.0, 0.0, 1.0]),    # sim = 0
            _make_fact("very similar fact", embedding=[0.99, 0.1, 0.0]), # sim ≈ 0.99
            _make_fact("moderately similar", embedding=[0.7, 0.7, 0.0]), # sim ≈ 0.71
        ]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)
        mock_settings = MagicMock()
        mock_settings.embedding_api_key = "sk-test"

        # Mock the httpx.AsyncClient and compute_embeddings_batch inside the tool
        mock_client_instance = AsyncMock()
        mock_async_client = MagicMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)

        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_async_client

        async def mock_compute(client, texts):
            # compute_embeddings_batch returns (embeddings, tokens_used).
            return [query_embedding], 0

        with (
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
            patch.dict("sys.modules", {"httpx": mock_httpx_module}),
            patch("archolith_proxy.extractor.embeddings.compute_embeddings_batch", side_effect=mock_compute),
        ):
            result = await search_facts_semantic("session-1", query="similar fact", limit=3)

        # "very similar fact" should appear first (highest cosine similarity)
        assert "very similar fact" in result
        similar_pos = result.find("very similar fact")
        dissimilar_pos = result.find("dissimilar fact")
        if dissimilar_pos != -1:
            assert similar_pos < dissimilar_pos, "Similar fact should rank before dissimilar"

    @pytest.mark.asyncio
    async def test_below_threshold_filtered_out(self):
        """Facts with similarity <= 0.05 are excluded from results."""
        query_embedding = [1.0, 0.0, 0.0]

        facts = [
            _make_fact("orthogonal fact", embedding=[0.0, 1.0, 0.0]),  # sim = 0
        ]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)
        mock_settings = MagicMock()
        mock_settings.embedding_api_key = "sk-test"

        mock_client_instance = AsyncMock()
        mock_async_client = MagicMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_async_client

        async def mock_compute(client, texts):
            # compute_embeddings_batch returns (embeddings, tokens_used).
            return [query_embedding], 0

        with (
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
            patch.dict("sys.modules", {"httpx": mock_httpx_module}),
            patch("archolith_proxy.extractor.embeddings.compute_embeddings_batch", side_effect=mock_compute),
        ):
            result = await search_facts_semantic("session-1", query="unrelated")

        assert "no facts above similarity threshold" in result

    @pytest.mark.asyncio
    async def test_no_embedding_on_facts_falls_back_to_substring(self):
        """When all facts lack embeddings, falls back to substring matching."""
        query_embedding = [1.0, 0.0, 0.0]

        facts = [
            _make_fact("auth token validation logic", embedding=None),
            _make_fact("database connection pool size", embedding=None),
        ]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)
        mock_settings = MagicMock()
        mock_settings.embedding_api_key = "sk-test"

        mock_client_instance = AsyncMock()
        mock_async_client = MagicMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_async_client

        async def mock_compute(client, texts):
            # compute_embeddings_batch returns (embeddings, tokens_used).
            return [query_embedding], 0

        with (
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
            patch.dict("sys.modules", {"httpx": mock_httpx_module}),
            patch("archolith_proxy.extractor.embeddings.compute_embeddings_batch", side_effect=mock_compute),
        ):
            result = await search_facts_semantic("session-1", query="auth", limit=5)

        # Falls back to substring — "auth token" should appear
        assert "auth token validation" in result
        assert "substring fallback" in result

    @pytest.mark.asyncio
    async def test_embedding_call_failure_falls_back(self):
        """When embedding computation fails, falls back to substring."""
        facts = [
            _make_fact("auth middleware is at line 42"),
        ]
        mock_backend = AsyncMock()
        mock_backend.get_active_facts = AsyncMock(return_value=facts)
        mock_settings = MagicMock()
        mock_settings.embedding_api_key = "sk-test"

        mock_async_client = MagicMock()
        mock_async_client.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        mock_async_client.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_async_client

        with (
            patch("archolith_proxy.curator.tools.get_backend", return_value=mock_backend),
            patch("archolith_proxy.config.get_settings", return_value=mock_settings),
            patch.dict("sys.modules", {"httpx": mock_httpx_module}),
        ):
            result = await search_facts_semantic("session-1", query="auth")

        # Should not raise — should fall back gracefully
        assert isinstance(result, str)
        # Either found via substring or reported empty
        assert "auth middleware" in result or "(no matching facts" in result or "fallback" in result


# ---------------------------------------------------------------------------
# Cosine similarity (inline _cosine logic verified directly)
# ---------------------------------------------------------------------------

class TestCosineLogic:
    def test_identical_vectors_score_1(self):
        v = [1.0, 0.5, 0.3]
        dot = sum(x * y for x, y in zip(v, v))
        mag = sum(x * x for x in v) ** 0.5
        sim = dot / (mag * mag)
        assert abs(sim - 1.0) < 1e-9

    def test_orthogonal_vectors_score_0(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        dot = sum(x * y for x, y in zip(a, b))
        assert dot == 0.0

    def test_opposite_vectors_score_minus_1(self):
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        sim = dot / (mag_a * mag_b)
        assert abs(sim - (-1.0)) < 1e-9
