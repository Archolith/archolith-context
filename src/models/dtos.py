"""Data transfer objects for assembly and extraction results."""

from __future__ import annotations

from pydantic import BaseModel


class AssembledContext(BaseModel):
    """What the assembler produces for the proxy to forward."""

    system_message: dict
    graph_context: list[dict]
    coherence_tail: list[dict]
    token_estimate: int = 0
    facts_retrieved: int = 0
    session_id: str = ""


class ExtractionResult(BaseModel):
    """What the extractor produces after parsing a response."""

    facts: list[dict]
    files_touched: list[str]
    decisions: list[dict]
    invalidated_fact_ids: list[str]
    turn_number: int
    session_goal: str | None = None
