"""Tests for memory adapter Pass 1 fixes (healthcheck, YAML escaping, close)."""

from __future__ import annotations

import pytest
import httpx

from archolith_proxy.memory.adapters.generic_http import Adapter as GenericHttpAdapter
from archolith_proxy.memory.adapters.basic_memory import Adapter as BasicMemoryAdapter
from archolith_proxy.memory.models import MemoryEngineConfig, PromotionOutcome


# ---------------------------------------------------------------------------
# 6.1: generic_http healthcheck returns False for 401/403/404/429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_http_healthcheck_401_unhealthy():
    """healthcheck() returns False for 401 Unauthorized."""
    config = MemoryEngineConfig(
        id="test-http",
        type="generic_http",
        base_url="http://localhost:9999",
        enabled=True,
        priority=10,
    )
    adapter = GenericHttpAdapter(config)

    # Mock client to return 401
    def mock_get(path):
        class MockResp:
            status_code = 401
        return MockResp()

    adapter._get_client = lambda: type('MockClient', (), {
        'get': lambda self, path: mock_get(path),
        'is_closed': False,
        'aclose': lambda self: None,
    })()

    result = await adapter.healthcheck()
    assert result is False


@pytest.mark.asyncio
async def test_generic_http_healthcheck_429_unhealthy():
    """healthcheck() returns False for 429 Too Many Requests."""
    config = MemoryEngineConfig(
        id="test-http",
        type="generic_http",
        base_url="http://localhost:9999",
        enabled=True,
        priority=10,
    )
    adapter = GenericHttpAdapter(config)

    def mock_get(path):
        class MockResp:
            status_code = 429
        return MockResp()

    adapter._get_client = lambda: type('MockClient', (), {
        'get': lambda self, path: mock_get(path),
        'is_closed': False,
        'aclose': lambda self: None,
    })()

    result = await adapter.healthcheck()
    assert result is False


@pytest.mark.asyncio
async def test_generic_http_healthcheck_200_healthy():
    """healthcheck() returns True for 200 OK."""
    config = MemoryEngineConfig(
        id="test-http",
        type="generic_http",
        base_url="http://localhost:9999",
        enabled=True,
        priority=10,
    )
    adapter = GenericHttpAdapter(config)

    def mock_get(path):
        class MockResp:
            status_code = 200
        return MockResp()

    adapter._get_client = lambda: type('MockClient', (), {
        'get': lambda self, path: mock_get(path),
        'is_closed': False,
        'aclose': lambda self: None,
    })()

    result = await adapter.healthcheck()
    assert result is True


# ---------------------------------------------------------------------------
# 6.2: basic_memory _escape_yaml escapes Windows paths
# ---------------------------------------------------------------------------


def test_basic_memory_escape_yaml_windows_path():
    """_escape_yaml escapes backslashes and quotes for Windows paths."""
    path = r'C:\Users\alice\project\main.py'
    escaped = BasicMemoryAdapter._escape_yaml(path)
    # Should escape backslashes first, then quotes
    assert '\\\\' in escaped or escaped.startswith('"')
    # Should be valid YAML value
    assert escaped != path  # Path was modified


def test_basic_memory_escape_yaml_simple_string():
    """_escape_yaml leaves simple strings unescaped."""
    text = "simple_observation"
    escaped = BasicMemoryAdapter._escape_yaml(text)
    assert escaped == text


def test_basic_memory_escape_yaml_with_colon():
    """_escape_yaml quotes strings with colons."""
    text = "decision: chose postgres"
    escaped = BasicMemoryAdapter._escape_yaml(text)
    assert escaped.startswith('"') and escaped.endswith('"')


def test_basic_memory_escape_yaml_with_newline():
    """_escape_yaml quotes strings with newlines."""
    text = "line1\nline2"
    escaped = BasicMemoryAdapter._escape_yaml(text)
    assert escaped.startswith('"') and escaped.endswith('"')


# ---------------------------------------------------------------------------
# Adapter close() safety tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generic_http_adapter_close_with_open_client():
    """close() properly closes an open httpx client."""
    config = MemoryEngineConfig(
        id="test-http",
        type="generic_http",
        base_url="http://localhost:9999",
        enabled=True,
        priority=10,
    )
    adapter = GenericHttpAdapter(config)

    # Create a real client to close
    adapter._client = httpx.AsyncClient()
    assert not adapter._client.is_closed

    await adapter.close()
    assert adapter._client.is_closed


@pytest.mark.asyncio
async def test_generic_http_adapter_close_idempotent():
    """close() is safe to call multiple times."""
    config = MemoryEngineConfig(
        id="test-http",
        type="generic_http",
        base_url="http://localhost:9999",
        enabled=True,
        priority=10,
    )
    adapter = GenericHttpAdapter(config)

    # Create a real client
    adapter._client = httpx.AsyncClient()

    # Close multiple times - should not raise
    await adapter.close()
    await adapter.close()
    assert adapter._client.is_closed


@pytest.mark.asyncio
async def test_generic_http_adapter_close_no_client():
    """close() is safe to call when no client exists."""
    config = MemoryEngineConfig(
        id="test-http",
        type="generic_http",
        base_url="http://localhost:9999",
        enabled=True,
        priority=10,
    )
    adapter = GenericHttpAdapter(config)

    # No client created yet
    assert not hasattr(adapter, '_client') or adapter._client is None

    # close() should not raise
    await adapter.close()
