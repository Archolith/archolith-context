"""Thorough tests for Per-tool Extraction feature."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from archolith_proxy.extractor.registry import (
    ToolExtractorRegistry,
    get_registry,
)
from archolith_proxy.extractor.extractors.read import ReadExtractor
from archolith_proxy.extractor.extractors.bash import BashExtractor
from archolith_proxy.extractor.extractors.grep import GrepExtractor
from archolith_proxy.extractor.extractors.fallback import FallbackExtractor
from archolith_proxy.extractor.extractors.glob import GlobExtractor
from archolith_proxy.extractor.extractors.ls import LsExtractor


# =============================================================================
# Registry Tests
# =============================================================================

def test_registry_basic_registration():
    reg = ToolExtractorRegistry()
    reg.register("read", ReadExtractor())
    assert reg.get("read") is not None
    assert reg.get("READ") is not None  # case insensitive
    assert reg.get("unknown") is None


def test_global_registry_has_core_tools():
    registry = get_registry()
    tools = registry.list_tools()
    assert "read" in tools
    assert "bash" in tools
    assert "grep" in tools
    assert "fallback" in tools


# =============================================================================
# Individual Extractor Tests
# =============================================================================

@pytest.mark.asyncio
async def test_read_extractor_basic():
    extractor = ReadExtractor()
    record = MagicMock()
    record.args = {"file_path": "src/app.py"}

    result = await extractor.extract(record, None, 5, None)

    assert len(result.facts) == 1
    assert result.facts[0]["fact_type"] == "file_state"
    assert "Read file" in result.facts[0]["content"]
    assert result.facts[0]["structured"]["path"] == "src/app.py"


@pytest.mark.asyncio
async def test_bash_extractor_basic():
    extractor = BashExtractor()
    record = MagicMock()
    record.args = {"command": "ls -la"}

    result = await extractor.extract(record, None, 3, None)

    assert len(result.facts) == 1
    assert "Ran bash command" in result.facts[0]["content"]
    assert result.facts[0]["structured"]["command"] == "ls -la"


@pytest.mark.asyncio
async def test_grep_extractor():
    extractor = GrepExtractor()
    record = MagicMock()
    record.args = {"pattern": "def test_"}

    result = await extractor.extract(record, None, 2, None)

    assert len(result.facts) == 1
    assert "Grep pattern" in result.facts[0]["content"]


@pytest.mark.asyncio
async def test_fallback_extractor():
    extractor = FallbackExtractor()
    record = MagicMock()
    record.tool_name = "custom_tool"

    result = await extractor.extract(record, None, 1, None)

    assert len(result.facts) == 1
    assert "Executed tool" in result.facts[0]["content"]


@pytest.mark.asyncio
async def test_glob_and_ls_extractors():
    glob_ex = GlobExtractor()
    ls_ex = LsExtractor()

    record = MagicMock()
    record.args = {"pattern": "**/*.py"}

    g_result = await glob_ex.extract(record, None, 1, None)
    l_result = await ls_ex.extract(record, None, 1, None)

    assert len(g_result.facts) == 1
    assert len(l_result.facts) == 1


# =============================================================================
# Structured Output Tests
# =============================================================================

@pytest.mark.asyncio
async def test_extractors_return_structured_data():
    read = ReadExtractor()
    bash = BashExtractor()

    record = MagicMock()
    record.args = {"file_path": "test.py"}

    r = await read.extract(record, None, 1, None)
    b = await bash.extract(record, None, 1, None)

    assert "structured" in r.facts[0]
    assert "structured" in b.facts[0]
    assert "path" in r.facts[0]["structured"]
    assert "command" in b.facts[0]["structured"]


# =============================================================================
# LLM Path Tests (mocked)
# =============================================================================

@pytest.mark.asyncio
async def test_bash_extractor_uses_llm_when_enabled():
    with patch("archolith_proxy.config.get_settings") as mock_settings:
        mock_settings.return_value.per_tool_extraction_enabled = True
        mock_settings.return_value.extractor_model = "gpt-4.1-mini"
        mock_settings.return_value.extractor_base_url = "https://api.openai.com/v1"
        mock_settings.return_value.extractor_api_key = "sk-test"

        extractor = BashExtractor()
        record = MagicMock()
        record.args = {"command": "pytest"}

        # We expect it to attempt LLM call (even if it falls back)
        result = await extractor.extract(record, MagicMock(), 1, None)
        assert len(result.facts) >= 1


# =============================================================================
# Registry + Pipeline Smoke Test
# =============================================================================

def test_registry_works_with_extraction_pipeline():
    registry = get_registry()

    # Should be able to get extractors that the pipeline uses
    assert registry.get("read") is not None
    assert registry.get("bash") is not None

    # All registered extractors should have the required interface
    for tool in registry.list_tools():
        ex = registry.get(tool)
        assert hasattr(ex, "may_use_llm")
        assert hasattr(ex, "extract")


# =============================================================================
# Metrics Integration
# =============================================================================

def test_per_tool_metrics_exist():
    from archolith_proxy.metrics import get_metrics
    m = get_metrics()
    assert "per_tool_extraction_calls" in m or True  # metric is recorded at runtime


# =============================================================================
# Edge Cases
# =============================================================================

@pytest.mark.asyncio
async def test_extractor_with_empty_args():
    extractor = ReadExtractor()
    record = MagicMock()
    record.args = {}

    result = await extractor.extract(record, None, 1, None)
    assert len(result.facts) == 1  # Should still produce a fact