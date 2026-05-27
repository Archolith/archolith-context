"""Stable public API surface for the archolith context engine.

Import from here rather than from internal submodules.
This module is the intended package boundary if proxy and engine are ever split.
"""

from archolith_proxy.extractor.base import (
    PartialExtractionResult as PartialExtractionResult,
    ToolCallRecord as ToolCallRecord,
    ToolExtractor as ToolExtractor,
)
from archolith_proxy.extractor.client import extract_facts_per_tool as extract_facts_per_tool
from archolith_proxy.extractor.registry import (
    get_registry as get_registry,
    register_extractor as register_extractor,
)
from archolith_proxy.memory.registry import (
    get_registry as get_memory_registry,
    register_memory_adapter as register_memory_adapter,
)
