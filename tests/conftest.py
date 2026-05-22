"""Shared test fixtures."""

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from archolith_proxy.config import reset_settings


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch):
    """Prevent pydantic-settings from reading the local .env file during tests.

    Without this, Settings() picks up developer overrides (e.g. compaction_enabled=True)
    which break tests that assert default values.
    """
    reset_settings()
    monkeypatch.setattr(
        "archolith_proxy.config.Settings.model_config",
        {**__import__("archolith_proxy.config", fromlist=["Settings"]).Settings.model_config, "env_file": None},
    )
    yield
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
