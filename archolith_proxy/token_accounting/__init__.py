"""Token accounting — explicit, comparable, inspectable token counts.

This package replaces the old single-number `_estimate_input_tokens` with
a multi-surface accounting system that tracks distinct notions of token size:

1. Content estimate — message content only (backward compatible)
2. Structural estimate — content + tools + tool_calls + framing
3. Client-reported — optional hint from the calling harness
4. Gate input — whichever surface the rewrite gate uses
5. Rewritten estimate — size after context assembly
6. Upstream usage — actual token count from the upstream API response

Every request produces a `TokenEstimateBreakdown` that is logged,
emitted via the live stream, and available in the per-turn trace.

The estimator uses tiktoken (cl100k_base) when available. Because tiktoken's
core releases the GIL, callers on the request hot path should invoke
`build_telemetry` via `asyncio.to_thread(...)` so encoding does not block the
event loop under concurrency.
"""

from archolith_proxy.token_accounting.client_hints import extract_client_hint
from archolith_proxy.token_accounting.estimate import (
    compute_savings,
    estimate_content_tokens,
    estimate_structural_tokens,
)
from archolith_proxy.token_accounting.gating import build_telemetry
from archolith_proxy.token_accounting.models import (
    GateDecision,
    GateSource,
    TokenEstimateBreakdown,
    TokenTelemetry,
)

__all__ = [
    "extract_client_hint",
    "compute_savings",
    "estimate_content_tokens",
    "estimate_structural_tokens",
    "build_telemetry",
    "GateDecision",
    "GateSource",
    "TokenEstimateBreakdown",
    "TokenTelemetry",
]
