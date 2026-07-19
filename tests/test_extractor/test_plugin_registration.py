"""Tests for extractor plugin registration via entry points and programmatic API."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.registry import ToolExtractorRegistry, get_registry


class _CustomExtractor(ToolExtractor):
    """A test extractor for an imaginary tool."""

    tool_names = ("custom_tool",)
    may_use_llm = False

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        return PartialExtractionResult(source_tool="custom_tool", facts=[], files_touched=[])


class _BashOverrideExtractor(ToolExtractor):
    """A test extractor that overrides the built-in BashExtractor."""

    tool_names = ("Bash",)
    may_use_llm = False

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        return PartialExtractionResult(source_tool="Bash", facts=[{
            "content": "[Bash] override",
            "fact_type": "tool_result",
            "confidence": 1.0,
        }], files_touched=[])


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the process-level singleton before and after each test."""
    import archolith_proxy.extractor.registry as reg_mod

    reg_mod._REGISTRY = None
    yield
    reg_mod._REGISTRY = None


def test_register_extractor_programmatic():
    """Programmatic registration adds an extractor to the singleton registry."""
    from archolith_proxy.extractor import register_extractor

    register_extractor(_CustomExtractor())

    reg = get_registry()
    extractor = reg.get("custom_tool")
    assert isinstance(extractor, _CustomExtractor)


def test_plugin_overrides_builtin():
    """A plugin extractor with the same tool_name overrides the built-in."""
    from archolith_proxy.extractor import register_extractor

    # Patch entry_points to return empty so we test against pure builtins,
    # not real venv plugins that may have been installed.
    with patch("importlib.metadata.entry_points", return_value=[]):
        # The built-in BashExtractor is now registered via build_default()
        reg = get_registry()
        builtin = reg.get("Bash")
        assert builtin.__class__.__name__ == "BashExtractor"

        # Register override — last-registered wins
        register_extractor(_BashOverrideExtractor())

        overridden = reg.get("Bash")
        assert isinstance(overridden, _BashOverrideExtractor)


def test_entry_point_discovery():
    """Mocked entry points are discovered and registered by build_default()."""
    fake_ep = MagicMock()
    fake_ep.name = "custom_via_ep"
    fake_ep.load.return_value = _CustomExtractor

    with patch("importlib.metadata.entry_points", return_value=[fake_ep]):
        reg = ToolExtractorRegistry.build_default()

    assert isinstance(reg.get("custom_tool"), _CustomExtractor)


def test_failed_plugin_load_does_not_crash():
    """A plugin entry point that raises on load is skipped — build_default() still succeeds."""
    bad_ep = MagicMock()
    bad_ep.name = "bad_plugin"
    bad_ep.load.side_effect = ImportError("missing dependency")

    with patch("importlib.metadata.entry_points", return_value=[bad_ep]):
        reg = ToolExtractorRegistry.build_default()

    # All built-ins should still be present
    assert reg.get("Bash").__class__.__name__ == "BashExtractor"
    assert reg.get("Read").__class__.__name__ == "ReadExtractor"


def test_engine_api_surface_importable():
    """Every symbol re-exported by archolith_proxy.engine is importable and not None."""
    from archolith_proxy.engine import (
        PartialExtractionResult,
        ToolCallRecord,
        ToolExtractor,
        extract_facts_per_tool,
        get_memory_registry,
        get_registry,
        register_extractor,
        register_memory_adapter,
    )

    assert ToolExtractor is not None
    assert ToolCallRecord is not None
    assert PartialExtractionResult is not None
    assert get_registry is not None
    assert register_extractor is not None
    assert extract_facts_per_tool is not None
    assert get_memory_registry is not None
    assert register_memory_adapter is not None


def test_registry_clear_removes_custom_extractors():
    """Registry.clear() removes all registered extractors."""
    from archolith_proxy.extractor import register_extractor

    # Register a custom extractor
    register_extractor(_CustomExtractor())

    reg = get_registry()
    # Verify it's registered
    ext = reg.get("custom_tool")
    assert isinstance(ext, _CustomExtractor)

    # Clear the registry
    reg.clear()

    # Now custom_tool should not be in the map (but get() will fall back to default)
    # Verify the internal map is empty
    assert len(reg._map) == 0
    # And default is cleared too
    assert reg._default is None
