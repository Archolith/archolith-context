"""Data transfer objects for assembly, extraction, and trace results."""

from __future__ import annotations

import time
import uuid
from pydantic import BaseModel, Field


class AssembledContext(BaseModel):
    """What the assembler produces for the proxy to forward."""

    system_message: dict
    graph_context: list[dict]
    coherence_tail: list[dict]
    token_estimate: int = 0
    facts_retrieved: int = 0
    session_id: str = ""
    files_selected: list[dict] = Field(default_factory=list)
    decisions_selected: list[dict] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """What the extractor produces after parsing a response."""

    facts: list[dict]
    files_touched: list[str]
    decisions: list[dict]
    invalidated_fact_ids: list[str] # Description strings, not actual IDs — matched via find_matching_fact_ids()
    turn_number: int
    session_goal: str | None = None


# ---------------------------------------------------------------------------
# Turn Trace DTOs — Observability Phase 1 (Trace Contract)
# ---------------------------------------------------------------------------

TRACE_VERSION = 1


class TurnTrace(BaseModel):
    """Canonical trace record for a single proxy turn.

    This is the primary inspection artifact: one per request flowing through
    the proxy. It captures what the proxy received, what it sent upstream,
    what it rewrote, and what it wrote back to the graph — enabling an
    operator to answer 'what did the proxy do on this turn?' without
    reading logs or querying Neo4j manually.
    """

    # Identity
    turn_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    session_id: str | None = None
    turn_number: int = 0
    trace_version: int = TRACE_VERSION
    created_at: float = Field(default_factory=time.time)

    # Request
    model: str = ""
    stream: bool = False
    input_tokens: int = 0
    message_count: int = 0

    # Assembly
    assembly_mode: str = "passthrough"
    assembly_reason: str = ""  # Human-readable reason for the mode choice
    assembly_latency_ms: float = 0.0

    # Token economics
    rewritten_tokens: int = 0
    savings_tokens: int = 0
    savings_ratio: float = 0.0

    # Facts selected for injection (from assembler)
    facts_selected: list[dict] = Field(default_factory=list)
    files_selected: list[dict] = Field(default_factory=list)
    decisions_selected: list[dict] = Field(default_factory=list)

    # Prompt payloads (original vs rewritten)
    original_messages: list[dict] = Field(default_factory=list)
    rewritten_messages: list[dict] = Field(default_factory=list)

    # Upstream response
    upstream_status: int = 0
    upstream_latency_ms: float = 0.0
    output_tokens: int | None = None
    upstream_response_summary: str = ""  # First 500 chars of response text

    # Extraction
    extraction_latency_ms: float = 0.0
    facts_stored: int = 0
    duplicates_skipped: int = 0
    invalidations_attempted: int = 0
    invalidations_matched: int = 0
    extracted_facts: list[dict] = Field(default_factory=list)

    # Recall
    recall_used: bool = False
    recall_question: str = ""
    recall_facts_returned: int = 0

    # Fallback
    fallback_reason: str = ""


class SessionTraceSummary(BaseModel):
    """Aggregated view of a session's trace history."""

    session_id: str
    goal: str | None = None
    turn_count: int = 0
    first_turn_at: float | None = None
    last_turn_at: float | None = None

    # Cumulative token economics
    total_input_tokens: int = 0
    total_savings_tokens: int = 0
    avg_savings_ratio: float = 0.0

    # Mode distribution
    assembly_modes: dict[str, int] = Field(default_factory=dict)

    # Fact counts
    total_facts_stored: int = 0
    total_duplicates_skipped: int = 0
    total_invalidations_attempted: int = 0
    total_recalls: int = 0
