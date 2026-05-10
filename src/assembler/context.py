"""Graph query → assembled message array.

The context assembler queries the session graph and produces a curated set of
messages that replace the linear middle portion of the conversation. It preserves
the system prompt and a configurable "coherence tail" of recent messages.

Assembly strategy:
1. Always include: session goal, recent decisions, touched files summary
2. Fill remaining budget with facts sorted by relevance:
   - Recent facts first (higher source_turn = more recent)
   - Higher confidence facts first
   - Priority types: error > state > file_state > tool_result > observation
3. Format as a single system-like message injected before the coherence tail
4. Estimate token count conservatively (~4 chars per token for code-heavy text)

Cold start: when turn_number < cold_start_turns AND total input tokens
< cold_start_token_threshold, the assembler returns None (passthrough mode).
"""

from __future__ import annotations

import structlog

from src.config import get_settings
from src.graph import facts as facts_repo
from src.graph import session as session_repo
from src.graph import edges as edges_repo
from src.graph import cleanup as cleanup_repo
from src.models.dtos import AssembledContext
from src.models.graph_nodes import FactType

logger = structlog.get_logger()

# Priority ordering for fact types (higher = more important)
_FACT_TYPE_PRIORITY = {
    FactType.ERROR: 5,
    FactType.GOAL: 4,
    FactType.STATE: 3,
    FactType.FILE_STATE: 3,
    FactType.TOOL_RESULT: 2,
    FactType.DECISION: 2,
    FactType.OBSERVATION: 1,
}


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate for code-heavy text.

    GPT-style tokenizers average ~4 chars/token for code.
    We use 3.5 to be conservative (overestimate token count).
    """
    return max(1, len(text) // 3)


def _format_context_block(
    goal: str | None,
    facts: list[dict],
    files: list[dict],
    decisions: list[dict],
    turn_number: int,
) -> str:
    """Format graph data into a structured context block.

    Returns a string suitable for injection as a system/context message.
    """
    parts = ["[Session Context — assembled from knowledge graph]"]
    parts.append("")

    # Session goal
    if goal:
        parts.append(f"## Session Goal\n{goal}")
        parts.append("")

    # Touched files summary
    if files:
        parts.append("## Files Touched")
        for f in files[:20]:  # Cap at 20 files
            path = f.get("path", f.get("s.path", "?"))
            status = f.get("status", f.get("s.status", "?"))
            parts.append(f"- {path} ({status})")
        parts.append("")

    # Decisions
    if decisions:
        parts.append("## Decisions Made")
        for d in decisions[:10]:  # Cap at 10 decisions
            summary = d.get("summary", d.get("d.summary", ""))
            rationale = d.get("rationale", d.get("d.rationale"))
            turn = d.get("turn", d.get("d.turn", "?"))
            line = f"- [turn {turn}] {summary}"
            if rationale:
                line += f" (rationale: {rationale})"
            parts.append(line)
        parts.append("")

    # Active facts (sorted by priority)
    sorted_facts = sorted(
        facts,
        key=lambda f: (
            _FACT_TYPE_PRIORITY.get(FactType(f.get("fact_type", "observation")), 0),
            f.get("confidence", 0.5),
            f.get("source_turn", 0),
        ),
        reverse=True,
    )

    if sorted_facts:
        parts.append("## Relevant Facts")
        for f in sorted_facts:
            content = f.get("content", "")
            fact_type = f.get("fact_type", "observation")
            confidence = f.get("confidence", 0.5)
            turn = f.get("source_turn", "?")
            # Compact format: type, turn, content (skip confidence for brevity)
            parts.append(f"- [{fact_type}|t{turn}] {content}")
        parts.append("")

    parts.append(f"[End of session context — current turn: {turn_number}]")

    return "\n".join(parts)


def _budget_facts(
    facts: list[dict],
    token_budget: int,
) -> list[dict]:
    """Select facts that fit within the token budget, prioritized by relevance.

    Budget is measured in tokens (~3.5 chars/token for code).
    """
    # Sort facts by priority: type priority > confidence > recency
    sorted_facts = sorted(
        facts,
        key=lambda f: (
            _FACT_TYPE_PRIORITY.get(FactType(f.get("fact_type", "observation")), 0),
            f.get("confidence", 0.5),
            f.get("source_turn", 0),
        ),
        reverse=True,
    )

    selected = []
    used_tokens = 0

    for fact in sorted_facts:
        content = fact.get("content", "")
        # Estimate tokens for this fact line: "- [type|tN] content\n"
        fact_line = f"- [{fact.get('fact_type', 'observation')}|t{fact.get('source_turn', '?')}] {content}\n"
        fact_tokens = _estimate_tokens(fact_line)

        if used_tokens + fact_tokens <= token_budget:
            selected.append(fact)
            used_tokens += fact_tokens
        else:
            # Budget exhausted
            break

    return selected


async def assemble_context(
    session_id: str,
    turn_number: int,
    input_token_estimate: int,
) -> AssembledContext | None:
    """Assemble context from the session graph for request rewriting.

    Returns None if the session should use linear passthrough (cold start).

    The assembly includes:
    - Session goal
    - Active (non-expired) facts, budgeted by priority
    - Touched files
    - Recent decisions

    Args:
        session_id: The session to assemble context for.
        turn_number: Current turn number for the session.
        input_token_estimate: Rough estimate of total input tokens in the request.

    Returns:
        AssembledContext with graph-derived messages, or None for cold-start passthrough.
    """
    settings = get_settings()

    # Cold start check: don't assemble until we have enough graph data
    if turn_number < settings.cold_start_turns and input_token_estimate < settings.cold_start_token_threshold:
        logger.debug(
            "cold_start_passthrough",
            session_id=session_id,
            turn=turn_number,
            estimated_tokens=input_token_estimate,
            threshold_turns=settings.cold_start_turns,
            threshold_tokens=settings.cold_start_token_threshold,
        )
        return None

    # Query session graph
    session_data = await session_repo.find_by_session_id(session_id)
    goal = session_data.get("goal") if session_data else None

    # Get all active facts
    all_facts = await facts_repo.get_active_facts(session_id, limit=200)
    if not all_facts and not goal:
        # No graph data at all — passthrough
        logger.debug("no_graph_data", session_id=session_id)
        return None

    # Get touched files
    files = await _get_touched_files(session_id)

    # Get decisions
    decisions = await _get_decisions(session_id)

    # Budget: reserve tokens for goal, files, decisions, framing
    # The fact budget is what remains after fixed overhead
    fixed_overhead = 200  # goal + files + decisions + framing tokens
    fact_budget = max(0, settings.context_token_budget - fixed_overhead)

    # Select facts within budget
    budgeted_facts = _budget_facts(all_facts, fact_budget)

    # Format the context block
    context_text = _format_context_block(
        goal=goal,
        facts=budgeted_facts,
        files=files,
        decisions=decisions,
        turn_number=turn_number,
    )

    context_tokens = _estimate_tokens(context_text)

    # Build the assembled context as synthetic messages
    graph_context = [
        {
            "role": "system",
            "content": context_text,
        }
    ]

    result = AssembledContext(
        system_message=graph_context[0],
        graph_context=graph_context,
        coherence_tail=[],  # Filled by the proxy handler
        token_estimate=context_tokens,
        facts_retrieved=len(budgeted_facts),
        session_id=session_id,
    )

    logger.info(
        "context_assembled",
        session_id=session_id,
        turn=turn_number,
        facts_available=len(all_facts),
        facts_selected=len(budgeted_facts),
        files=len(files),
        decisions=len(decisions),
        token_estimate=context_tokens,
        budget=settings.context_token_budget,
    )

    return result


async def _get_touched_files(session_id: str) -> list[dict]:
    """Get all files touched in a session."""
    from src.graph.repository import run_query, CONTEXT_SESSION_LABEL

    cypher = f"""
MATCH (s:{CONTEXT_SESSION_LABEL}:Session {{session_id: $session_id}})-[:TOUCHES]->(f:{CONTEXT_SESSION_LABEL}:File)
RETURN f.path AS path, f.status AS status, f.last_modified_turn AS last_modified_turn, f.last_read_turn AS last_read_turn
ORDER BY f.last_modified_turn DESC, f.last_read_turn DESC
"""
    results = await run_query(cypher, {"session_id": session_id})
    return results


async def _get_decisions(session_id: str) -> list[dict]:
    """Get all decisions for a session (non-superseded)."""
    from src.graph.repository import run_query, CONTEXT_SESSION_LABEL

    cypher = f"""
MATCH (d:{CONTEXT_SESSION_LABEL}:Decision {{session_id: $session_id}})
WHERE d.superseded_by IS NULL
RETURN d.summary AS summary, d.rationale AS rationale, d.turn AS turn
ORDER BY d.turn DESC
"""
    results = await run_query(cypher, {"session_id": session_id})
    return results
