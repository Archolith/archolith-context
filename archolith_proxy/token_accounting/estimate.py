"""Token estimation — structural estimator covering all request surfaces.

The old _estimate_input_tokens() counted only message content. This module
adds estimates for:

- Tool schemas in the `tools` array
- Assistant tool_calls (function name + arguments)
- Tool message payloads (tool results)
- Message framing overhead (role, name, timestamps, etc.)
- Multipart message content (image_url, file, etc.)

All estimates use tiktoken cl100k_base with a 10% margin, matching the
project convention. The 500-token floor is preserved for backward compat.

The estimator returns a TokenEstimateBreakdown rather than a single int,
so callers can see exactly what was counted.
"""

from __future__ import annotations

import structlog

from archolith_proxy.token_accounting.models import (
    TokenEstimateBreakdown,
    GateSource,
    GateDecision,
)

logger = structlog.get_logger()

# Per-message framing overhead estimate (role, separators, metadata).
# Empirically, each message adds ~4-6 tokens of framing on top of content.
# We use 6 tokens as a conservative estimate.
_MESSAGE_FRAMING_TOKENS = 6

# Per tool definition overhead (name, description boilerplate, separators)
_TOOL_DEFINITION_FRAMING_TOKENS = 8

# Version tag for the estimator
ESTIMATOR_VERSION = "v2-structural"


def _encode_count(text: str) -> int:
    """Count tokens in a string using cl100k_base with 10% margin, min 1."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        raw = len(enc.encode(text))
        return max(int(raw * 1.10), 1)
    except ImportError:
        # Fallback: ~3.6 chars per token + 10% margin
        return max(int(len(text) / 3.6 * 1.10), 1)


def estimate_content_tokens(messages: list[dict]) -> int:
    """Estimate tokens from message content fields only.

    This is the legacy _estimate_input_tokens behavior, preserved
    for backward compatibility and comparison.
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multipart content
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if not text and part.get("type") == "image_url":
                        # Image URL: estimate from the URL string
                        text = part.get("image_url", {}).get("url", "")
                    total += _encode_count(text) if text else 0
        elif isinstance(content, str):
            total += _encode_count(content)
    return max(int(total * 1.10), 500)


def estimate_structural_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> int:
    """Estimate total request tokens including structural overhead.

    This covers:
    - All message content (same as content estimate)
    - Message framing (role, metadata, separators)
    - Tool definitions in the `tools` array
    - Assistant tool_calls (function name + arguments)
    - Tool message payloads (tool results)
    - Multipart content (image URLs, files)

    This is the "full picture" estimate that should be materially closer
    to what the upstream API actually charges for.
    """
    total = 0

    # 1. Message content (same as content estimate)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if not text and part.get("type") == "image_url":
                        text = part.get("image_url", {}).get("url", "")
                    total += _encode_count(text) if text else 0
        elif isinstance(content, str):
            total += _encode_count(content)

        # 2. Message framing overhead
        total += _MESSAGE_FRAMING_TOKENS

        # 3. Tool name (for tool-role messages)
        name = msg.get("name")
        if name:
            total += _encode_count(name)

        # 4. Assistant tool_calls
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    total += _encode_count(func.get("name", ""))
                    total += _encode_count(func.get("arguments", ""))
                    # Tool call ID overhead
                    total += 4  # id + type framing

        # 5. Tool call ID on tool messages
        tool_call_id = msg.get("tool_call_id")
        if tool_call_id:
            total += _encode_count(tool_call_id)

    # 6. Tool definitions
    if tools and isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                func = tool.get("function", {})
                total += _encode_count(func.get("name", ""))
                total += _encode_count(func.get("description", "") or "")
                # Parameters JSON schema
                params = func.get("parameters")
                if params and isinstance(params, dict):
                    import json
                    total += _encode_count(json.dumps(params))
                total += _TOOL_DEFINITION_FRAMING_TOKENS

    return max(int(total * 1.10), 500)


def estimate_rewritten_tokens(messages: list[dict]) -> int:
    """Estimate tokens in a rewritten messages array (after context assembly).

    Same logic as structural estimate — the rewritten payload still has
    the same structure, just different content.
    """
    return estimate_structural_tokens(messages)


def compute_breakdown(
    messages: list[dict],
    tools: list[dict] | None = None,
    client_reported_tokens: int | None = None,
    session_id: str = "",
    turn_number: int = 0,
) -> TokenEstimateBreakdown:
    """Compute a full token estimate breakdown for a request.

    This is the main entry point for token accounting. It produces
    content-only, structural, and (optionally) client-reported estimates,
    then determines which surface to use for rewrite gating.

    Args:
        messages: The messages array from the request.
        tools: The tools array from the request (may be None).
        client_reported_tokens: Optional token count from the calling harness.
        session_id: Session ID for traceability.
        turn_number: Turn number for traceability.

    Returns:
        TokenEstimateBreakdown with all estimates populated.
    """
    content_est = estimate_content_tokens(messages)
    structural_est = estimate_structural_tokens(messages, tools)

    # Gate input: use max of structural and client-reported
    if client_reported_tokens is not None:
        gate_input = max(structural_est, client_reported_tokens)
        gate_source = GateSource.MAX_STRUCTURAL_CLIENT
    else:
        gate_input = structural_est
        gate_source = GateSource.STRUCTURAL_ESTIMATE

    return TokenEstimateBreakdown(
        input_tokens_content_est=content_est,
        input_tokens_structural_est=structural_est,
        input_tokens_client_reported=client_reported_tokens,
        gate_input_tokens=gate_input,
        gate_source=gate_source,
        session_id=session_id,
        turn_number=turn_number,
        estimator_version=ESTIMATOR_VERSION,
    )


def compute_savings(
    breakdown: TokenEstimateBreakdown,
    rewritten_messages: list[dict],
    graph_context_tokens: int = 0,
) -> TokenEstimateBreakdown:
    """Update a breakdown with rewrite estimates and savings.

    Args:
        breakdown: The pre-rewrite breakdown.
        rewritten_messages: The messages array after context assembly.
        graph_context_tokens: Tokens in the graph-assembled context block.

    Returns:
        Updated breakdown with rewritten, savings, and savings_ratio fields.
    """
    rewritten_est = estimate_rewritten_tokens(rewritten_messages)
    savings = max(0, breakdown.gate_input_tokens - rewritten_est)
    savings_ratio = savings / max(breakdown.gate_input_tokens, 1)

    breakdown.rewritten_tokens_est = rewritten_est
    breakdown.graph_context_tokens_est = graph_context_tokens
    breakdown.savings_tokens_est = savings
    breakdown.savings_ratio_est = savings_ratio

    return breakdown


def evaluate_gate(
    breakdown: TokenEstimateBreakdown,
    min_input_tokens: int = 55000,
    min_savings_ratio: float = 0.25,
    cold_start_turns: int = 3,
    cold_start_token_threshold: int = 20000,
    turn_number: int = 0,
) -> GateDecision:
    """Decide whether a request should be rewritten based on token accounting.

    This replaces the inline gate logic in chat.py with a well-documented,
    testable function that uses the token breakdown explicitly.

    Args:
        breakdown: The token estimate breakdown for this request.
        min_input_tokens: Minimum gate_input_tokens to consider rewriting.
        min_savings_ratio: Minimum savings_ratio to justify rewriting.
        cold_start_turns: Turns below this are always passthrough (if under token threshold).
        cold_start_token_threshold: Token threshold for cold-start bypass.
        turn_number: Current turn number.

    Returns:
        GateDecision with the decision and reasoning.
    """
    gate_input = breakdown.gate_input_tokens

    # Cold start: don't rewrite until we have enough graph data
    if turn_number < cold_start_turns and gate_input < cold_start_token_threshold:
        return GateDecision(
            gate_source=breakdown.gate_source,
            gate_input_tokens=gate_input,
            min_input_tokens=min_input_tokens,
            min_savings_ratio=min_savings_ratio,
            savings_ratio=breakdown.savings_ratio_est,
            result="cold_start",
            reason=f"turn {turn_number} < {cold_start_turns} and {gate_input} < {cold_start_token_threshold} tokens",
        )

    # Low tokens: don't rewrite small conversations
    if gate_input < min_input_tokens:
        return GateDecision(
            gate_source=breakdown.gate_source,
            gate_input_tokens=gate_input,
            min_input_tokens=min_input_tokens,
            min_savings_ratio=min_savings_ratio,
            savings_ratio=breakdown.savings_ratio_est,
            result="skipped_low_tokens",
            reason=f"gate_input {gate_input} < min {min_input_tokens} (source: {breakdown.gate_source.value})",
        )

    # Low savings: rewriting doesn't save enough
    if breakdown.savings_ratio_est < min_savings_ratio:
        return GateDecision(
            gate_source=breakdown.gate_source,
            gate_input_tokens=gate_input,
            min_input_tokens=min_input_tokens,
            min_savings_ratio=min_savings_ratio,
            savings_ratio=breakdown.savings_ratio_est,
            result="skipped_low_savings",
            reason=f"savings_ratio {breakdown.savings_ratio_est:.1%} < {min_savings_ratio:.0%}",
        )

    # All gates passed — rewrite
    return GateDecision(
        gate_source=breakdown.gate_source,
        gate_input_tokens=gate_input,
        min_input_tokens=min_input_tokens,
        min_savings_ratio=min_savings_ratio,
        savings_ratio=breakdown.savings_ratio_est,
        result="graph",
        reason=f"gate_input {gate_input} >= {min_input_tokens}, savings {breakdown.savings_ratio_est:.1%} >= {min_savings_ratio:.0%}",
    )
