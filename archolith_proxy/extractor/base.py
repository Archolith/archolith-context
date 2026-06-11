"""Base types for the per-tool extraction system.

ToolCallRecord   — one tool invocation (name, args, filtered result).
PartialExtractionResult — facts/files produced by one ToolExtractor.
ToolExtractor    — abstract base class; subclasses handle specific tool types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

__all__ = [
    "ToolCallRecord",
    "PartialExtractionResult",
    "ToolExtractor",
]


@dataclass
class ToolCallRecord:
    """One tool invocation: name, args, and the raw result string."""

    tool_call_id: str
    tool_name: str
    args: dict
    result: str  # content after tool result filtering (Layer 1)


@dataclass
class PartialExtractionResult:
    """Facts/files produced by a single ToolExtractor.

    Each extractor prefixes its fact content with ``"[tool_name] "`` so the
    source is visible in the graph without requiring a schema change.
    """

    source_tool: str
    facts: list[dict] = field(default_factory=list)
    # Each fact: {content, fact_type, confidence}
    files_touched: list[str] = field(default_factory=list)
    used_llm: bool = False
    # Token usage from LLM calls (populated by LLM-backed extractors)
    usage: dict = field(default_factory=dict)


class ToolExtractor(ABC):
    """Abstract base for per-tool extractors.

    Subclasses declare ``tool_names`` — the registry uses these for routing.
    Prefix-sentinel names (e.g. ``"mcp__memory__recall"``) enable prefix-match
    routing for tool families that share a common namespace prefix.
    """

    tool_names: tuple[str, ...] = ()
    may_use_llm: bool = False  # True for extractors that make API calls (BashExtractor,
    # WebFetchExtractor, DefaultExtractor). The orchestrator uses this to decide
    # whether to gate the extractor behind the LLM concurrency semaphore.

    @abstractmethod
    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        ...
