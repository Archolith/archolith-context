"""Re-export public API from extractor and memory registries.

This module bundles key types and factory functions from the extractor and
memory backends for convenient import. It serves as a stable boundary between
the proxy core and the backend components.

Exports:
- Extractor types: PartialExtractionResult, ToolCallRecord, ToolExtractor
- Extractor client: extract_facts_per_tool
- Registry access: get_registry, register_extractor, get_memory_registry, register_memory_adapter
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
