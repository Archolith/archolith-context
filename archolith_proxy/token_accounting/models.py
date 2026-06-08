"""Token accounting models — DTOs for token estimates, telemetry, and gate decisions.

Glossary of token surfaces:
  - content: tokens from message content fields only
  - structural: content + tool schemas + tool_calls + framing overhead
  - client_reported: optional hint from the calling harness (e.g., OpenCode session size)
  - gate_input: the specific surface used for rewrite gating decisions
  - rewritten: estimated size after context assembly
  - graph_context: tokens in the graph-assembled context block
  - savings: tokens saved by rewriting (gate_input - rewritten)
  - savings_ratio: savings / gate_input
  - upstream_usage: actual prompt_tokens from the upstream API response
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class GateSource(str, Enum):
    """Which token surface was used for the rewrite gate decision."""
    STRUCTURAL_ESTIMATE = "structural_estimate"
    CONTENT_ESTIMATE = "content_estimate"
    CLIENT_REPORTED = "client_reported"
    MAX_STRUCTURAL_CLIENT = "max_structural_client"


class GateDecision(BaseModel):
    """Record of why a request was or was not rewritten."""

    gate_source: GateSource = GateSource.STRUCTURAL_ESTIMATE
    gate_input_tokens: int = 0
    min_input_tokens: int = 0
    min_savings_ratio: float = 0.0
    savings_ratio: float = 0.0
    result: str = "passthrough"  # cold_start, skipped_low_tokens, skipped_low_savings, graph, fallback, passthrough
    reason: str = ""


class TokenEstimateBreakdown(BaseModel):
    """Full breakdown of token estimates for a single request/turn.

    All estimates use cl100k_base tiktoken encoding with 10% margin
    unless otherwise noted.
    """

    # --- Input estimates ---
    input_tokens_content_est: int = 0
    """Tokens from message content fields only. Backward-compatible with
    the old _estimate_input_tokens()."""

    input_tokens_structural_est: int = 0
    """Content + tools array + tool_calls + message framing overhead.
    This is the most accurate proxy-side estimate of real request size."""

    input_tokens_client_reported: int | None = None
    """Optional token count reported by the calling harness.
    Stored separately — never overwrites proxy estimates."""

    # --- Gate decision ---
    gate_input_tokens: int = 0
    """The token count actually used for rewrite gating.
    Defaults to max(structural, client_reported) when available."""

    gate_source: GateSource = GateSource.STRUCTURAL_ESTIMATE
    """Which surface was used for the gate decision."""

    # --- Rewrite estimates ---
    rewritten_tokens_est: int = 0
    """Estimated tokens after context assembly rewriting."""

    graph_context_tokens_est: int = 0
    """Tokens in the graph-assembled context block."""

    # --- Savings ---
    savings_tokens_est: int = 0
    """gate_input_tokens - rewritten_tokens_est (only positive values)."""

    savings_ratio_est: float = 0.0
    """savings_tokens_est / gate_input_tokens."""

    # --- Output ---
    output_tokens_upstream: int | None = None
    """Actual prompt_tokens from the upstream API response, if available."""

    # --- Metadata ---
    session_id: str = ""
    turn_number: int = 0
    estimator_version: str = "v2-structural"

    @property
    def effective_input(self) -> int:
        """The authoritative input token count for this request.

        Uses max(structural, client_reported) when client hint is available,
        otherwise falls back to structural estimate.
        """
        if self.input_tokens_client_reported is not None:
            return max(self.input_tokens_structural_est, self.input_tokens_client_reported)
        return self.input_tokens_structural_est


class TokenTelemetry(BaseModel):
    """Per-turn telemetry record for observability and debugging.

    This is what gets logged, streamed, and stored in traces.
    It includes the full breakdown plus the gate decision context.
    """

    breakdown: TokenEstimateBreakdown = Field(default_factory=TokenEstimateBreakdown)
    gate_decision: GateDecision = Field(default_factory=GateDecision)

    # Assembly details
    assembly_mode: str = "unknown"  # cold_start, graph, fallback, passthrough, etc.
    assembly_latency_ms: float = 0.0
    extraction_latency_ms: float = 0.0

    # Fact counts
    facts_available: int = 0
    facts_selected: int = 0

    def to_log_dict(self) -> dict:
        """Flatten to a structured log dict (no nested objects for structlog)."""
        b = self.breakdown
        g = self.gate_decision
        return {
            "session_id": b.session_id,
            "turn": b.turn_number,
            "estimator": b.estimator_version,
            "input_content": b.input_tokens_content_est,
            "input_structural": b.input_tokens_structural_est,
            "input_client": b.input_tokens_client_reported,
            "gate_input": b.gate_input_tokens,
            "gate_source": b.gate_source.value,
            "rewritten": b.rewritten_tokens_est,
            "graph_ctx": b.graph_context_tokens_est,
            "savings": b.savings_tokens_est,
            "savings_ratio": round(b.savings_ratio_est, 3),
            "output_upstream": b.output_tokens_upstream,
            "assembly_mode": self.assembly_mode,
            "assembly_ms": round(self.assembly_latency_ms, 1),
            "extraction_ms": round(self.extraction_latency_ms, 1),
            "facts_available": self.facts_available,
            "facts_selected": self.facts_selected,
            "gate_result": g.result,
            "gate_reason": g.reason,
        }
