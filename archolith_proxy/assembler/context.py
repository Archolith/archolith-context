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

from archolith_proxy.config import get_settings
from archolith_proxy.graph.backend import get_backend

from archolith_proxy.assembler.compress import compress_facts_batch
from archolith_proxy.assembler.intent import (
    TurnIntent, classify_intent, DOMAIN_TO_FACT_TYPES,
)
from archolith_proxy.models.dtos import AssembledContext
from archolith_proxy.models.graph_nodes import FactType

logger = structlog.get_logger()

# In-memory cache for user message embeddings (keyed by SHA-256 hash)
# Each entry: (embedding_vector, insertion_time)
_embedding_cache: dict[str, tuple[list[float], float]] = {}
_EMBEDDING_CACHE_MAX = 128
_EMBEDDING_CACHE_TTL_S = 3600  # 1 hour TTL

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
    intent: TurnIntent | None = None,
) -> float:
    """Score a fact for relevance using intent-driven weighting.

    Scoring formula (with intent):
      intent_match (30%) + similarity (25%) + recency (20%) + type+confidence (25%)

    Without intent, falls back to:
      similarity (40%) + recency (30%) + type+confidence (30%)

    Recency uses logarithmic decay instead of linear — a turn-1 fact at
    turn 20 scores ~0.38 (log) instead of 0.05 (linear).
    """
    import math

    fact_type_str = fact.get("fact_type", "observation")
    try:
        type_priority = _FACT_TYPE_PRIORITY.get(FactType(fact_type_str), 0)
    except ValueError:
        type_priority = 0
    type_score = type_priority / 5.0

    confidence = fact.get("confidence", 0.5)

    # Logarithmic recency: decays slowly, old facts remain usable
    source_turn = fact.get("source_turn", 0)
    if turn_number > 0 and source_turn > 0:
        age = turn_number - source_turn
        recency = 1.0 / (1.0 + math.log1p(age))
    else:
        recency = 0.5

    # Embedding similarity
    fact_embedding = fact.get("embedding")
    if query_embedding and fact_embedding:
        similarity = _cosine_similarity(query_embedding, fact_embedding)
    else:
        similarity = 0.0

    # Intent match: boost facts whose type aligns with the detected intent
    intent_boost = 0.0
    if intent and intent.domain_weights:
        for domain, weight in intent.domain_weights.items():
            matching_types = DOMAIN_TO_FACT_TYPES.get(domain, [])
            if fact_type_str in matching_types:
                intent_boost = max(intent_boost, weight)

        # Explicit reference match: if the fact mentions a referenced file/identifier
        if intent.explicit_refs:
            content_lower = fact.get("content", "").lower()
            for ref in intent.explicit_refs:
                if ref.lower() in content_lower:
                    intent_boost = max(intent_boost, 0.8)
                    break

    if intent and intent.domain_weights:
        # Intent-driven scoring
        return (
            intent_boost * 0.30
            + similarity * 0.25
            + recency * 0.20
            + (type_score * 0.5 + confidence * 0.5) * 0.25
        )
    elif query_embedding and fact_embedding:
        return similarity * 0.40 + recency * 0.30 + (type_score * 0.5 + confidence * 0.5) * 0.30
    else:
        return type_score * 0.40 + confidence * 0.30 + recency * 0.30


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
) -> tuple[str, float]:
    """Format the per-turn relevant facts section with compression.

    Compresses each fact to its densest usable form at render time.
    Sorted by priority, then confidence, then recency.

    Returns:
        Tuple of (formatted_text, compression_ratio).
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

    compression_ratio = 1.0
    if sorted_facts:
        compressed, compression_ratio = compress_facts_batch(sorted_facts)
        for f in compressed:
            content = f.get("content", "")
            fact_type = f.get("fact_type", "observation")
            turn = f.get("source_turn", "?")
            parts.append(f"- [{fact_type}|t{turn}] {content}")
    else:
        parts.append("(no facts above relevance threshold)")

    parts.append("")
    return "\n".join(parts), compression_ratio


def _format_context_block(
    goal: str | None,
    facts: list[dict],
    files: list[dict],
    decisions: list[dict],
    turn_number: int,
    active_fact_count: int = 0,
) -> tuple[str, float]:
    """Format graph data into a structured context block with two tiers.

    Tier 1 — Session Overview (stable, cacheable):
    Session goal, files touched, decisions, fact count.

    Tier 2 — Relevant Facts (per-turn, query-dependent):
    Budgeted facts sorted by relevance, compressed at render time.

    Returns:
        Tuple of (formatted_block, compression_ratio).
    """
    overview = _format_session_overview(goal, files, decisions, turn_number, active_fact_count)
    facts_section, compression_ratio = _format_relevant_facts(facts, turn_number)

    block = overview + "\n" + facts_section + f"[End of session context — current turn: {turn_number}]"
    return block, compression_ratio


def _budget_facts(
    facts: list[dict],
    token_budget: int,
    query_embedding: list[float] | None = None,
    turn_number: int = 0,
    embedding_enabled: bool = False,
    intent: TurnIntent | None = None,
) -> list[dict]:
    """Select facts within token budget using intent-driven scoring.

    Strategy:
    1. Anchor facts (goal, active decisions) are always included first
    2. Remaining budget filled by intent-scored facts
    3. N-1/N+1 context windowing expands to preserve narrative continuity

    When intent is available, facts are scored with intent-match boosting.
    Otherwise falls back to embedding/priority scoring.
    """
    effective_turn = turn_number
    if effective_turn <= 0 and facts:
        effective_turn = max(f.get("source_turn", 0) for f in facts) or 1

    use_embeddings = embedding_enabled and query_embedding is not None

    # Step 1: Separate anchor facts (goal, decision) — always included
    anchors = []
    candidates = []
    for fact in facts:
        ft = fact.get("fact_type", "observation")
        if ft in ("goal", "decision"):
            anchors.append(fact)
        else:
            candidates.append(fact)

    # Budget anchors first
    selected = []
    used_tokens = 0
    for fact in anchors:
        content = fact.get("content", "")
        fact_line = f"- [{fact.get('fact_type', 'observation')}|t{fact.get('source_turn', '?')}] {content}\n"
        fact_tokens = _estimate_tokens(fact_line)
        if used_tokens + fact_tokens <= token_budget:
            selected.append(fact)
            used_tokens += fact_tokens

    remaining_budget = token_budget - used_tokens

    # Step 2: Score and select remaining facts
    scored = []
    for fact in candidates:
        score = _score_fact(
            fact,
            query_embedding if use_embeddings else None,
            effective_turn,
            intent=intent,
        )
        scored.append((score, fact))

    scored.sort(key=lambda x: x[0], reverse=True)

    for score, fact in scored:
        content = fact.get("content", "")
        fact_line = f"- [{fact.get('fact_type', 'observation')}|t{fact.get('source_turn', '?')}] {content}\n"
        fact_tokens = _estimate_tokens(fact_line)
        if used_tokens + fact_tokens <= token_budget:
            selected.append(fact)
            used_tokens += fact_tokens
        else:
            break

    # Step 3: Context windowing (narrative continuity)
    if use_embeddings and selected:
        windowed = _expand_with_context_window(selected, facts)
        total_tokens = 0
        final = []
        for fact in windowed:
            content = fact.get("content", "")
            fact_line = f"- [{fact.get('fact_type', 'observation')}|t{fact.get('source_turn', '?')}] {content}\n"
            fact_tokens = _estimate_tokens(fact_line)
            if total_tokens + fact_tokens <= token_budget:
                final.append(fact)
                total_tokens += fact_tokens
        if len(final) >= len(selected):
            selected = final

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

    # Cold start check: don't assemble until we have enough real user turns.
    # Agentic clients (OpenCode, Claude Code) send multiple API requests per
    # user turn (one per tool-call round trip), so the backend's request
    # counter inflates quickly. Count actual user-role messages instead.
    user_turn_count = sum(1 for m in (messages or []) if m.get("role") == "user")
    if user_turn_count < settings.cold_start_turns and input_token_estimate < settings.cold_start_token_threshold:
        logger.debug(
            "cold_start_passthrough",
            session_id=session_id,
            turn=turn_number,
            user_turns=user_turn_count,
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
            from archolith_proxy.assembler.query_rewrite import needs_rewrite, rewrite_query, extract_recent_exchanges
            rewritten = None
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
        session_data = await get_backend().find_session_by_id(session_id)
    except Exception as e:
        logger.warning("graph_query_failed_session", session_id=session_id, error=str(e))
        return None

    goal = session_data.get("goal") if session_data else None

    # Get all active facts
    try:
        all_facts = await get_backend().get_active_facts(session_id, limit=200)
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

    # Intent analysis — drives fact selection
    intent = classify_intent(
        user_message=user_message or "",
        session_goal=goal,
        recent_messages=messages[-6:] if messages else None,
    )
    logger.debug(
        "intent_classified",
        session_id=session_id,
        turn=turn_number,
        question_type=intent.question_type.value,
        domains=[d.value for d in intent.domains],
        explicit_refs=intent.explicit_refs[:5],
        is_topic_shift=intent.is_topic_shift,
        confidence=round(intent.confidence, 2),
    )

    # Budget: reserve tokens for goal, files, decisions, framing
    fixed_overhead = 200
    fact_budget = max(0, settings.context_token_budget - fixed_overhead)

    # Intent-driven fact selection
    budgeted_facts = _budget_facts(
        all_facts,
        fact_budget,
        query_embedding=query_embedding,
        turn_number=turn_number,
        embedding_enabled=settings.embedding_enabled,
        intent=intent,
    )

    # Format the context block with two tiers (overview + compressed facts)
    context_text, compression_ratio = _format_context_block(
        goal=goal,
        facts=budgeted_facts,
        files=files,
        decisions=decisions,
        turn_number=turn_number,
        active_fact_count=len(all_facts),
    )

    context_tokens = _estimate_tokens(context_text)

    graph_context = [
        {
            "role": "system",
            "content": context_text,
        }
    ]

    result = AssembledContext(
        system_message=graph_context[0],
        graph_context=graph_context,
        coherence_tail=[],
        token_estimate=context_tokens,
        facts_retrieved=len(budgeted_facts),
        session_id=session_id,
        files_selected=files,
        decisions_selected=decisions,
        compression_ratio=compression_ratio,
    )

    logger.info(
        "context_assembled",
        session_id=session_id,
        turn=turn_number,
        intent_type=intent.question_type.value,
        intent_domains=[d.value for d in intent.domains],
        facts_available=len(all_facts),
        facts_selected=len(budgeted_facts),
        files=len(files),
        decisions=len(decisions),
        token_estimate=context_tokens,
        budget=settings.context_token_budget,
        compression_ratio=round(compression_ratio, 2),
    )

    return result


async def _get_query_embedding(
    user_message: str,
    http_client=None,
) -> list[float] | None:
    """Compute embedding for a user message, with simple caching.

    Uses an in-memory cache keyed by SHA-256 hash to avoid
    re-computing embeddings for identical queries (retries, etc).
    Entries are TTL-bounded (1 hour) and size-bounded (128 entries).
    """
    import hashlib
    import time

    from archolith_proxy.extractor.embeddings import compute_embeddings_batch

    cache_key = hashlib.sha256(user_message.encode()).hexdigest()[:16]

    # Check in-memory cache (with TTL check)
    if cache_key in _embedding_cache:
        embedding, inserted_at = _embedding_cache[cache_key]
        if time.monotonic() - inserted_at < _EMBEDDING_CACHE_TTL_S:
            return embedding
        # Expired — remove
        del _embedding_cache[cache_key]

    if http_client is None:
        return None

    try:
        results = await compute_embeddings_batch(http_client, [user_message[:8000]])
        embedding = results[0] if results else None
        if embedding is not None:
            _embedding_cache[cache_key] = (embedding, time.monotonic())
            # Evict oldest entries if cache grows too large
            if len(_embedding_cache) > _EMBEDDING_CACHE_MAX:
                _evict_embedding_cache()
        return embedding
    except Exception as e:
        logger.warning("query_embedding_compute_failed", error=str(e))
        return None


def _evict_embedding_cache() -> None:
    """Evict expired and excess embedding cache entries.

    1. Remove all entries older than TTL.
    2. If still over max, remove the oldest remaining entries (FIFO).
    """
    import time

    now = time.monotonic()
    # Purge expired entries
    expired_keys = [
        k for k, (_, ts) in _embedding_cache.items()
        if now - ts >= _EMBEDDING_CACHE_TTL_S
    ]
    for k in expired_keys:
        del _embedding_cache[k]

    # If still over limit, remove oldest
    if len(_embedding_cache) > _EMBEDDING_CACHE_MAX:
        sorted_keys = sorted(_embedding_cache, key=lambda k: _embedding_cache[k][1])
        excess = len(_embedding_cache) - _EMBEDDING_CACHE_MAX
        for k in sorted_keys[:excess]:
            del _embedding_cache[k]


async def _get_touched_files(session_id: str) -> list[dict]:
    """Get all files touched in a session."""
    return await get_backend().get_touched_files(session_id)


async def _get_decisions(session_id: str) -> list[dict]:
    """Get all decisions for a session (non-superseded)."""
    return await get_backend().get_decisions(session_id, include_superseded=False)
