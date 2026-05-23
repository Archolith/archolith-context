"""CuratorResult — structured return type from the curator loop."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CuratorResult:
    """Structured result from the curator LLM loop.

    Maps to AssembledContext fields for injection into the proxy pipeline.
    """

    context_text: str           # The formatted context block to inject
    curated_paths: set[str] = field(default_factory=set)   # Files the curator retrieved
    tool_calls_used: int = 0    # How many tool calls the loop made
    estimated_tokens: int = 0   # tiktoken estimate of context_text
