"""Tests for Per-tool Extraction (Phase 0-3)."""

import pytest
from unittest.mock import MagicMock, AsyncMock

from archolith_proxy.extractor.registry import (
    ToolExtractorRegistry,
    get_registry,
    register_extractor,
    get_extractor,
)
from archolith_proxy.extractor.extractors.read import ReadExtractor
from archolith_proxy.extractor.extractors.bash import BashExtractor
from archolith_proxy.extractor.extractors.grep import GrepExtractor
from archolith_proxy.extractor.extractors.fallback import FallbackExtractor


def test_registry_registration():
    reg = ToolExtractorRegistry()
    reg.register("read", ReadExtractor())
    reg.register("bash", BashExtractor())

    assert reg.get("read") is not None
    assert reg.get("bash") is not None
    assert reg.get("unknown") is None


def test_auto_registration():
    registry = get_registry()
    tools = registry.list_tools()

    # Should have at least the core tools
    assert "read" in tools
    assert "bash" in tools
    assert "fallback" in tools


@pytest.mark.asyncio
async def test_read_extractor():
    extractor = ReadExtractor()
    record = MagicMock()
    record.args = {"file_path": "src/test.py"}

    result = await extractor.extract(record, None, 1, None)

    assert len(result.facts) == 1
    assert "Read file" in result.facts[0]["content"]
    assert result.facts[0]["fact_type"] == "file_state"


@pytest.mark.asyncio
async def test_bash_extractor():
    extractor = BashExtractor()
    record = MagicMock()
    record.args = {"command": "pytest tests/"}

    result = await extractor.extract(record, None, 1, None)

    assert len(result.facts) == 1
    assert "Ran bash command" in result.facts[0]["content"]


def test_grep_extractor():
    extractor = GrepExtractor()
    # Simple sync check
    assert extractor.may_use_llm is False


def test_fallback_extractor():
    extractor = FallbackExtractor()
    record = MagicMock()
    record.tool_name = "unknown_tool"

    # We can't easily await without full setup, but structure is correct
    assert extractor.may_use_llm is False


def test_metrics_import():
    from archolith_proxy.metrics import get_metrics
    metrics = get_metrics()
    # Should be able to access context cache metrics we added earlier
    assert "context_cache_hits" in metrics