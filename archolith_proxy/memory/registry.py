"""Memory engine registry — load, instantiate, and look up memory adapters.

The registry is populated from config (env / JSON) and provides lookup by id,
type, or default target. Adapters are instantiated lazily on first access.
"""

from __future__ import annotations

import importlib
import structlog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archolith_proxy.memory.adapters.base import MemoryAdapterBase
    from archolith_proxy.memory.models import MemoryEngineConfig

logger = structlog.get_logger()

# Module-level singleton
_registry: MemoryEngineRegistry | None = None

# Adapter type → module path mapping
_ADAPTER_TYPES: dict[str, str] = {
    "archolith_memory": "archolith_proxy.memory.adapters.archolith_memory",
    "mem0": "archolith_proxy.memory.adapters.mem0",
    "zep": "archolith_proxy.memory.adapters.zep",
    "generic_http": "archolith_proxy.memory.adapters.generic_http",
    "basic_memory": "archolith_proxy.memory.adapters.basic_memory",
    "claude_mem": "archolith_proxy.memory.adapters.claude_mem",
    "cognee": "archolith_proxy.memory.adapters.cognee",
    "openmemory": "archolith_proxy.memory.adapters.openmemory",
    "nocturne_memory": "archolith_proxy.memory.adapters.nocturne_memory",
}


class MemoryEngineRegistry:
    """Holds configured memory engines and resolves adapters for promotion."""

    def __init__(self) -> None:
        self._engines: dict[str, MemoryEngineConfig] = {}
        self._adapters: dict[str, MemoryAdapterBase] = {}
        self._default_engine_id: str | None = None

    @property
    def engine_count(self) -> int:
        return len(self._engines)

    @property
    def default_engine_id(self) -> str | None:
        return self._default_engine_id

    # --- Registration ---

    def register(self, config: MemoryEngineConfig) -> None:
        """Register an engine from its config. Does not instantiate the adapter yet."""
        if config.id in self._engines:
            logger.warning("memory_engine_overwrite", engine_id=config.id)
        self._engines[config.id] = config

        # Track default: highest priority among enabled engines
        if config.enabled:
            if self._default_engine_id is None or config.priority > self._engines.get(self._default_engine_id, config).priority:
                self._default_engine_id = config.id

        logger.info(
            "memory_engine_registered",
            engine_id=config.id,
            engine_type=config.type,
            enabled=config.enabled,
            priority=config.priority,
        )

    def load_from_config(self, configs: list[MemoryEngineConfig]) -> None:
        """Register multiple engines from a config list."""
        for cfg in configs:
            self.register(cfg)

    # --- Lookup ---

    def get_config(self, engine_id: str) -> MemoryEngineConfig | None:
        """Get engine config by id."""
        return self._engines.get(engine_id)

    def get_adapter(self, engine_id: str) -> MemoryAdapterBase | None:
        """Get or instantiate the adapter for an engine. Returns None if not found or disabled."""
        config = self._engines.get(engine_id)
        if config is None:
            return None
        if not config.enabled:
            logger.debug("memory_engine_disabled", engine_id=engine_id)
            return None

        # Lazy instantiation
        if engine_id not in self._adapters:
            adapter = self._instantiate_adapter(config)
            if adapter is not None:
                self._adapters[engine_id] = adapter
            else:
                return None

        return self._adapters[engine_id]

    def get_default_adapter(self) -> MemoryAdapterBase | None:
        """Get the adapter for the default engine (highest priority, enabled)."""
        if self._default_engine_id is None:
            return None
        return self.get_adapter(self._default_engine_id)

    def list_engines(self) -> list[dict]:
        """Return summary info for all registered engines."""
        return [
            {
                "id": cfg.id,
                "type": cfg.type,
                "enabled": cfg.enabled,
                "priority": cfg.priority,
                "base_url": cfg.base_url,
                "is_default": cfg.id == self._default_engine_id,
            }
            for cfg in self._engines.values()
        ]

    # --- Lifecycle ---

    async def healthcheck_all(self) -> dict[str, bool]:
        """Run healthcheck on all enabled engines. Returns engine_id → healthy."""
        results: dict[str, bool] = {}
        for engine_id, config in self._engines.items():
            if not config.enabled:
                continue
            adapter = self.get_adapter(engine_id)
            if adapter is None:
                results[engine_id] = False
                continue
            try:
                healthy = await adapter.healthcheck()
                results[engine_id] = healthy
            except Exception:
                logger.exception("healthcheck_failed", engine_id=engine_id)
                results[engine_id] = False
        return results

    # --- Internal ---

    def _instantiate_adapter(self, config: MemoryEngineConfig) -> MemoryAdapterBase | None:
        """Dynamically import and instantiate the adapter class for an engine type."""
        module_path = _ADAPTER_TYPES.get(config.type)
        if module_path is None:
            logger.error("unknown_engine_type", engine_id=config.id, engine_type=config.type)
            return None

        try:
            module = importlib.import_module(module_path)
        except ImportError:
            logger.exception("adapter_import_failed", engine_id=config.id, module=module_path)
            return None

        # Convention: adapter module exposes `Adapter` class
        adapter_cls = getattr(module, "Adapter", None)
        if adapter_cls is None:
            logger.error("adapter_class_missing", engine_id=config.id, module=module_path)
            return None

        try:
            return adapter_cls(config)
        except Exception:
            logger.exception("adapter_instantiation_failed", engine_id=config.id)
            return None


def get_registry() -> MemoryEngineRegistry:
    """Return the global registry singleton."""
    global _registry
    if _registry is None:
        _registry = MemoryEngineRegistry()
    return _registry


def reset_registry() -> None:
    """Reset the registry — used in tests."""
    global _registry
    _registry = None
