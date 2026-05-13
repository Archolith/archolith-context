"""Graph query → assembled message array.

The context assembler queries the session graph and produces a curated set of
messages that replace the linear middle portion of the conversation. It preserves
the system prompt and a configurable "coherence tail" of recent messages.

Assembly strategy:
1. Always include: session goal, recent decisions, touched files summary
2. Fill remaining budget with facts sorted by relevance:
   - When embeddings available: cosine similarity (40%) + recency (30%) + type+confidence (30%)
   - Without embeddings: type priority > confidence > recency (current behavior)
   - N-1/N+1 context windowing expands selection to include adjacent-turn facts
3. Format as a single system-like message injected before the coherence tail
4. Estimate token count using tiktoken cl100k_base

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

# In-memory cache for user message embeddings (keyed by SHA-256 hash)
_embedding_cache: dict[str, list[float]] = {}

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
    """Token estimate using cl100k_base with 10% margin.

    Uses tiktoken for accurate counts, then adds a 10% safety margin.
    Minimum of 1 token for empty strings.
    """
    import tiktoken
    raw = len(tiktoken.get_encoding("cl100k_base").encode(text))
    with_margin = int(raw * 1.10)
    return max(with_margin, 1)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 if either vector is empty or has zero magnitude.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _score_fact(
    fact: dict,
    query_embedding: list[float] | None,
    turn_number: int,
) -> float:
    """Score a fact for relevance to the current query.

    When embeddings are available:
      similarity (40%) + recency (30%) + type+confidence (30%)

    Without embeddings (fallback):
      type priority (40%) + confidence (30%) + recency (30%)
    """
    # Normalize type priority to 0-1 range
    fact_type_str = fact.get("fact_type", "observation")
    try:
        type_priority = _FACT_TYPE_PRIORITY.get(FactType(fact_type_str), 0)
    except ValueError:
        type_priority = 0
    type_score = type_priority / 5.0  # max priority is 5

    confidence = fact.get("confidence", 0.5)

    # Recency: source_turn / turn_number (0-1, higher = more recent)
    source_turn = fact.get("source_turn", 0)
    recency = source_turn / max(turn_number, 1)

    fact_embedding = fact.get("embedding")

    if query_embedding and fact_embedding:
        similarity = _cosine_similarity(query_embedding, fact_embedding)
        # Weighted blend: similarity 40%, recency 30%, type+confidence 30%
        return similarity * 0.4 + recency * 0.3 + (type_score * 0.5 + confidence * 0.5) * 0.3
    else:
        # No embeddings: fall back to priority/recency/confidence
        return type_score * 0.4 + confidence * 0.3 + recency * 0.3


def _expand_with_context_window(
    selected_facts: list[dict],
    all_facts: list[dict],
) -> list[dict]:
    """Expand selected facts with N-1/N+1 context windowing.

    For each selected fact, include facts from adjacent turns
    (source_turn - 1, source_turn + 1) that aren't already selected.
    This preserves narrative continuity (error → fix, question → decision).
    """
    if not selected_facts:
        return selected_facts

    selected_ids = {f.get("fact_id") for f in selected_facts}
    selected_turns = {f.get("source_turn") for f in selected_facts}

    # Build the window of adjacent turns
    window_turns = set()
    for t in selected_turns:
        if t is not None:
            window_turns.update({t - 1, t, t + 1})

    # Add facts from window turns that aren't already selected
    additional = []
    for f in all_facts:
        if f.get("fact_id") not in selected_ids and f.get("source_turn") in window_turns:
            additional.append(f)

    return selected_facts + additional


def _format_session_overview(
    goal: str | None,
    files: list[dict],
    decisions: list[dict],
    turn_number: int,
    active_fact_count: int = 0,
) -> str:
    """Format the stable session overview section.

    This section is intended to remain stable across turns so that API
    prompt caching can reuse it. It contains structural knowledge that
    rarely changes: goal, files, decisions, and a fact count summary.
    """
    parts = ["=== SESSION OVERVIEW ==="]
    parts.append("")

    if goal:
        parts.append(f"## Session Goal\n{goal}")
        parts.append("")

    if files:
        parts.append("## Files Touched")
        for f in files[:20]:
            path = f.get("path", f.get("s.path", "?"))
            status = f.get("status", f.get("s.status", "?"))
            parts.append(f"- {path} ({status})")
        parts.append("")

    if decisions:
        parts.append("## Decisions Made")
        for d in decisions[:10]:
            summary = d.get("summary", d.get("d.summary", ""))
            rationale = d.get("rationale", d.get("d.rationale"))
            turn = d.get("turn", d.get("d.turn", "?"))
            line = f"- [turn {turn}] {summary}"
            if rationale:
                line += f" (rationale: {rationale})"
            parts.append(line)
        parts.append("")

    parts.append(f"Knowledge base: {active_fact_count} active facts | Current turn: {turn_number}")
    parts.append("")

    return "\n".join(parts)


def _format_relevant_facts(
    facts: list[dict],
    turn_number: int,
) -> str:
    """Format the per-turn relevant facts section.

    This section changes every turn as different facts are relevant.
    Sorted by priority, then confidence, then recency.
    """
    parts = ["=== RELEVANT CONTEXT ==="]
    parts.append("")

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
        for f in sorted_facts:
            content = f.get("content", "")
            fact_type = f.get("fact_type", "observation")
            turn = f.get("source_turn", "?")
            parts.append(f"- [{fact_type}|t{turn}] {content}")
    else:
        parts.append("(no facts above relevance threshold)")

    parts.append("")
    return "\n".join(parts)


def _format_context_block(
    goal: str | None,
    facts: list[dict],
    files: list[dict],
    decisions: list[dict],
    turn_number: int,
    active_fact_count: int = 0,
) -> str:
    """Format graph data into a structured context block with two tiers.

    Tier 1 — Session Overview (stable, cacheable):
    Session goal, files touched, decisions, fact count.

    Tier 2 — Relevant Facts (per-turn, query-dependent):
    Budgeted facts sorted by relevance.

    The overview section comes first to benefit from prompt caching
    (stable prefix across turns). The facts section follows.
    """
    overview = _format_session_overview(goal, files, decisions, turn_number, active_fact_count)
    facts_section = _format_relevant_facts(facts, turn_number)

    return overview + "\n" + facts_section + f"[End of session context — current turn: {turn_number}]"


def _budget_facts(
    facts: list[dict],
    token_budget: int,
    query_embedding: list[float] | None = None,
    turn_number: int = 0,
    embedding_enabled: bool = False,
) -> list[dict]:
    """Select facts that fit within the token budget, scored by relevance.

    When embeddings are available and enabled, facts are scored using
    cosine similarity + recency + type/confidence. Otherwise falls back
    to priority-only scoring (current behavior).

    After initial selection, N-1/N+1 context windowing expands the selection
    to include facts from adjacent turns.
    """
    # Infer turn_number from facts if not provided (avoids division issues)
    effective_turn = turn_number
    if effective_turn <= 0 and facts:
        effective_turn = max(f.get("source_turn", 0) for f in facts) or 1

    # Score facts
    use_embeddings = embedding_enabled and query_embedding is not None
    scored_facts = []
    for fact in facts:
        if use_embeddings:
            score = _score_fact(fact, query_embedding, effective_turn)
        else:
            score = _score_fact(fact, None, effective_turn)
        scored_facts.append((score, fact))

    # Sort by score descending
    scored_facts.sort(key=lambda x: x[0], reverse=True)

    # Select facts within budget
    selected = []
    used_tokens = 0

    for score, fact in scored_facts:
        content = fact.get("content", "")
        fact_line = f"- [{fact.get('fact_type', 'observation')}|t{fact.get('source_turn', '?')}] {content}\n"
        fact_tokens = _estimate_tokens(fact_line)

        if used_tokens + fact_tokens <= token_budget:
            selected.append(fact)
            used_tokens += fact_tokens
        else:
            break

    # Expand with N-1/N+1 context windowing if embeddings are active
    if use_embeddings and selected:
        windowed = _expand_with_context_window(selected, facts)
        # Re-budget: windowing may have added facts that exceed budget
        # Keep the windowed set but trim if it exceeds budget
        total_tokens = 0
        final = []
        for fact in windowed:
            content = fact.get("content", "")
            fact_line = f"- [{fact.get('fact_type', 'observation')}|t{fact.get('source_turn', '?')}] {content}\n"
            fact_tokens = _estimate_tokens(fact_line)
            if total_tokens + fact_tokens <= token_budget:
                final.append(fact)
                total_tokens += fact_tokens
            # If windowed facts exceed budget, stop adding — but keep
            # the originally selected core facts even if windowing trimmed
        if len(final) >= len(selected):
            selected = final
        # If windowing would shrink below original, keep original (shouldn't happen)

    return selected


async def assemble_context(
    session_id: str,
    turn_number: int,
    input_token_estimate: int,
    user_message: str | None = None,
    http_client=None,
    messages: list[dict] | None = None,
) -> AssembledContext | None:
    """Assemble context from the session graph for request rewriting.

    Returns None if the session should use linear passthrough (cold start).

    Graceful degradation: if Neo4j is unreachable, returns None (passthrough)
    instead of raising. The caller always falls back to linear passthrough.

    When embedding_enabled is True and a user_message is provided, the
    assembler computes a query embedding and uses cosine similarity for
    relevance scoring. Otherwise falls back to priority/recency.

    The assembly includes:
    - Session goal
    - Active (non-expired) facts, budgeted by relevance
    - Touched files
    - Recent decisions

    Args:
        session_id: The session to assemble context for.
        turn_number: Current turn number for the session.
        input_token_estimate: Rough estimate of total input tokens in the request.
        user_message: The current user message (used for embedding-based retrieval).

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

    # Query rewriting: resolve ambiguous references before embedding
    effective_query = user_message
    if (
        settings.query_rewrite_enabled
        and settings.embedding_enabled
        and user_message
        and http_client
    ):
        try:
                from src.assembler.query_rewrite import needs_rewrite, rewrite_query, extract_recent_exchanges
                if needs_rewrite(user_message):
                    # Extract recent user/assistant exchanges for reference resolution
                    recent = extract_recent_exchanges(messages or [], max_exchanges=3)
                    rewritten = await rewrite_query(http_client, user_message, recent)
                if rewritten:
                    effective_query = rewritten
                    logger.debug(
                        "query_rewritten_for_embedding",
                        session_id=session_id,
                        turn=turn_number,
                        original=user_message[:60],
                        rewritten=rewritten[:60],
                    )
        except Exception as e:
            logger.warning("query_rewrite_error", session_id=session_id, error=str(e))

    # Compute query embedding if embedding-driven retrieval is enabled
    query_embedding = None
    if settings.embedding_enabled and settings.embedding_api_key and effective_query:
        try:
            query_embedding = await _get_query_embedding(effective_query, http_client)
            logger.debug(
                "query_embedding_computed",
                session_id=session_id,
                turn=turn_number,
                has_embedding=query_embedding is not None,
                query_rewritten=effective_query != user_message,
            )
        except Exception as e:
            logger.warning("query_embedding_failed", session_id=session_id, error=str(e))

    # Query session graph — each query is wrapped for graceful degradation
    try:
        session_data = await session_repo.find_by_session_id(session_id)
    except Exception as e:
        logger.warning("graph_query_failed_session", session_id=session_id, error=str(e))
        return None

    goal = session_data.get("goal") if session_data else None

    # Get all active facts
    try:
        all_facts = await facts_repo.get_active_facts(session_id, limit=200)
    except Exception as e:
        logger.warning("graph_query_failed_facts", session_id=session_id, error=str(e))
        all_facts = []

    if not all_facts and not goal:
        # No graph data at all — passthrough
        logger.debug("no_graph_data", session_id=session_id)
        return None

    # Get touched files
    try:
        files = await _get_touched_files(session_id)
    except Exception as e:
        logger.warning("graph_query_failed_files", session_id=session_id, error=str(e))
        files = []

    # Get decisions
    try:
        decisions = await _get_decisions(session_id)
    except Exception as e:
        logger.warning("graph_query_failed_decisions", session_id=session_id, error=str(e))
        decisions = []

    # Budget: reserve tokens for goal, files, decisions, framing
    # The fact budget is what remains after fixed overhead
    fixed_overhead = 200  # goal + files + decisions + framing tokens
    fact_budget = max(0, settings.context_token_budget - fixed_overhead)

    # Select facts within budget — use embedding scoring if available
    budgeted_facts = _budget_facts(
        all_facts,
        fact_budget,
        query_embedding=query_embedding,
        turn_number=turn_number,
        embedding_enabled=settings.embedding_enabled,
    )

    # Format the context block with two tiers (overview + relevant facts)
    context_text = _format_context_block(
        goal=goal,
        facts=budgeted_facts,
        files=files,
        decisions=decisions,
        turn_number=turn_number,
        active_fact_count=len(all_facts),
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
        files_selected=files,
        decisions_selected=decisions,
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


async def _get_query_embedding(
    user_message: str,
    http_client=None,
) -> list[float] | None:
    """Compute embedding for a user message, with simple caching.

    Uses an in-memory cache keyed by SHA-256 hash to avoid
    re-computing embeddings for identical queries (retries, etc).
    """
    import hashlib
    from src.extractor.embeddings import compute_embeddings_batch

    cache_key = hashlib.sha256(user_message.encode()).hexdigest()[:16]

    # Check in-memory cache
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]

    if http_client is None:
        return None

    try:
        results = await compute_embeddings_batch(http_client, [user_message[:8000]])
        embedding = results[0] if results else None
        if embedding is not None:
            _embedding_cache[cache_key] = embedding
            # Evict oldest entries if cache grows too large
            if len(_embedding_cache) > 64:
                _evict_embedding_cache()
        return embedding
    except Exception as e:
        logger.warning("query_embedding_compute_failed", error=str(e))
        return None


def _evict_embedding_cache() -> None:
    """Evict half the embedding cache entries (simple FIFO eviction)."""
    keys = list(_embedding_cache.keys())
    for key in keys[: len(keys) // 2]:
        del _embedding_cache[key]


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
