"""Gate logic — decide whether to rewrite based on token accounting.

This module provides the top-level function that the chat handler calls
to produce a complete TokenTelemetry for each request. It combines:
- Structural estimation (from estimate.py)
- Client hints (from client_hints.py)
- Gate decision (from evaluate_gate in estimate.py)

The chat handler can then use the telemetry for logging, live streaming,
and metrics without needing to understand the internals.
"""

from __future__ import annotations

import structlog

from archolith_proxy.token_accounting.models import TokenTelemetry
from archolith_proxy.token_accounting.estimate import (
    compute_breakdown,
    evaluate_gate,
)

logger = structlog.get_logger()


def build_telemetry(
    messages: list[dict],
    tools: list[dict] | None = None,
    client_reported_tokens: int | None = None,
    session_id: str = "",
    turn_number: int = 0,
    min_input_tokens: int = 55000,
    min_savings_ratio: float = 0.25,
    cold_start_turns: int = 3,
    cold_start_token_threshold: int = 20000,
) -> TokenTelemetry:
    """Build complete token telemetry for a request (pre-rewrite).

    This is the primary entry point for the chat handler. It produces
    the breakdown and gate decision but does NOT compute savings
    (which requires the rewritten messages).

    Call compute_savings() after rewriting to fill in the savings fields.

    Args:
        messages: The messages array from the request.
        tools: The tools array from the request.
        client_reported_tokens: Optional token count from the client.
        session_id: Session ID for traceability.
        turn_number: Current turn number.
        min_input_tokens: Minimum input tokens to consider rewriting.
        min_savings_ratio: Minimum savings ratio to justify rewriting.
        cold_start_turns: Cold-start turn threshold.
        cold_start_token_threshold: Cold-start token threshold.

    Returns:
        TokenTelemetry with breakdown and gate decision.
    """
    breakdown = compute_breakdown(
        messages=messages,
        tools=tools,
        client_reported_tokens=client_reported_tokens,
        session_id=session_id,
        turn_number=turn_number,
    )

    gate = evaluate_gate(
        breakdown=breakdown,
        min_input_tokens=min_input_tokens,
        min_savings_ratio=min_savings_ratio,
        cold_start_turns=cold_start_turns,
        cold_start_token_threshold=cold_start_token_threshold,
        turn_number=turn_number,
    )

    return TokenTelemetry(
        breakdown=breakdown,
        gate_decision=gate,
        assembly_mode=gate.result,
    )
