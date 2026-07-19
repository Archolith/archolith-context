"""Per-extraction LLM budget shared by per-tool and turn-level calls."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class LLMBudgetExceeded(RuntimeError):
    """Raised before an upstream call when the per-turn budget is exhausted."""


class ExtractionBudget:
    max_llm_calls: int
    max_requested_tokens: int
    llm_calls: int = 0
    requested_tokens: int = 0

    def reserve(self, requested_tokens: int) -> bool:
        """Reserve a bounded request before making an upstream LLM call."""
        if self.llm_calls >= self.max_llm_calls:
            return False
        if self.requested_tokens + requested_tokens > self.max_requested_tokens:
            return False
        self.llm_calls += 1
        self.requested_tokens += requested_tokens
        return True


_current_budget: ContextVar[ExtractionBudget | None] = ContextVar(
    "extraction_budget", default=None,
)


def set_budget(budget: ExtractionBudget):
    return _current_budget.set(budget)


def reset_budget(token) -> None:
    _current_budget.reset(token)


def reserve_llm_call(requested_tokens: int) -> bool:
    """Return true when the current turn may issue another LLM request.

    Extractors remain usable standalone (no context means no budget), while the
    production per-tool orchestrator installs a budget for the whole operation.
    """
    budget = _current_budget.get()
    allowed = budget is None or budget.reserve(requested_tokens)
    if not allowed:
        raise LLMBudgetExceeded("per-turn extractor LLM budget exhausted")
    return True
