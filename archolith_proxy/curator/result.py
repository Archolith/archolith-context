"""CuratorResult — structured return type from the curator loop."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field as PydanticField


@dataclass
class CuratorToolCall:
    """Single tool call record from the curator loop."""

    tool: str
    args: dict = field(default_factory=dict)
    status: str = "ok"        # "ok" or "error"
    error: str = ""           # error message if status == "error"
    result_preview: str = ""  # first 200 chars of result (for debugging)
    raw_result: str = ""      # full result text (for briefing fidelity)

    def to_dict(self) -> dict:
        d: dict = {"tool": self.tool, "status": self.status}
        if self.args:
            d["args"] = self.args
        if self.error:
            d["error"] = self.error
        if self.result_preview:
            d["result_preview"] = self.result_preview
        # raw_result excluded from to_dict — too large for traces/diagnostics
        return d


@dataclass
class CuratorResult:
    """Structured result from the curator LLM loop.

    Maps to AssembledContext fields for injection into the proxy pipeline.
    """

    context_text: str           # The formatted context block to inject
    curated_paths: set[str] = field(default_factory=set)   # Files the curator retrieved
    tool_calls_used: int = 0    # How many tool calls the loop made
    estimated_tokens: int = 0   # tiktoken estimate of context_text
    # Turn numbers the curator selected to retain in the middle section.
    # None = keep all (curator did not call select_relevant_turns).
    retained_turn_numbers: list[int] | None = None
    # Per-call tool log — every tool dispatch (success and failure)
    tool_log: list[CuratorToolCall] = field(default_factory=list)


class CuratorFailure(BaseModel):
    """Diagnostic record saved when the curator fails to produce a context block.

    Captures the full curator-LLM conversation (system prompt, user prompt,
    tool calls, tool results, LLM responses) and the failure reason so
    patterns can be analyzed and the curator prompt improved.

    Persisted as JSONL in <trace_dir>/curator_failures.jsonl.
    """

    session_id: str
    failure_reason: str             # e.g. empty_response, llm_error, empty_final, context_length, unexpected_finish, stuck_loop, max_iterations
    messages: list[dict] = PydanticField(default_factory=list)
    tool_calls_made: int = 0
    curated_paths: list[str] = PydanticField(default_factory=list)
    retained_turn_numbers: list[int] | None = None
    iterations_completed: int = 0
    error_detail: str = ""
    timestamp: float = PydanticField(default_factory=time.time)
