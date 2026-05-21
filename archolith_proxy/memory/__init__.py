"""Memory engine registration and outbound promotion adapters.

This package implements the promotion boundary between the context-engine
proxy's session-local curation and durable external memory systems. The proxy
decides *what* is worth promoting; adapters decide *how* to write it.
"""

from archolith_proxy.memory.models import (
    EngineCapabilities,
    MemoryEngineConfig,
    PromotionOutcome,
    PromotionRecord,
    PromotionResult,
)
from archolith_proxy.memory.registry import MemoryEngineRegistry, get_registry

__all__ = [
    "EngineCapabilities",
    "MemoryEngineConfig",
    "MemoryEngineRegistry",
    "PromotionOutcome",
    "PromotionRecord",
    "PromotionResult",
    "get_registry",
]
