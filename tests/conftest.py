"""Shared test fixtures."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.config import reset_settings, set_session_settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch, request):
    """Prevent developer/runtime config from leaking into the test suite.

    Two sources would otherwise flip settings out from under tests:
    1. The local .env file (e.g. compaction_enabled=True).
    2. config_overrides.json — runtime overrides persisted by PATCH /admin/config and
       re-applied on top of env in get_settings(). A benchmark run that sets, say,
       session_recall_tool_enabled=false silently disables recall for the whole suite.
    Neutralize both so tests run against deterministic config.

    Tests that exercise the override-persistence mechanism itself opt out of the
    config_overrides neutralization with @pytest.mark.real_config_overrides.
    """
    reset_settings()
    monkeypatch.setattr(
        "archolith_proxy.config.Settings.model_config",
        {**__import__("archolith_proxy.config", fromlist=["Settings"]).Settings.model_config, "env_file": None},
    )
    if request.node.get_closest_marker("real_config_overrides") is None:
        monkeypatch.setattr("archolith_proxy.config._read_overrides", lambda: {})
    # Clear any per-session settings overlay so it can't leak between tests.
    set_session_settings(None)
    yield
    set_session_settings(None)
    reset_settings()


@pytest.fixture
def app():
    """Create a test app instance."""
    from archolith_proxy.main import create_app
    return create_app()


@pytest.fixture
async def client(app):
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_upstream_response():
    """A standard non-streaming upstream response."""
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello!"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
