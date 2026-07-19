"""Deterministic inline assembler — LLM-free hot-path read (Phase 2).

The two_curator assembler (``run_assembler``) makes a synchronous LLM call on the
request hot path to reformat the prepper's briefing and select turns. Phase 2 of
the event-driven curator-worker plan removes that call: the prepper already
produced a structured ``SessionBriefing`` (the typed pools — goal, current state,
open issues, last verification, decisions, facts, pre-fetched files, retained
turns), so the inline read can compose the context block **deterministically in
pure code** and fit it to a token budget. No LLM, no 3s race, no fall-through.

Budget policy: the small high-value pools (goal, state, issues, verification,
decisions, facts) are kept verbatim; the elastic ``RELEVANT CODE`` pool fills the
remaining budget and is truncated (fence-closed) when it would overflow. This
mirrors the heterogeneous-per-type retention idea in the plan (small pools kept,
code paged to budget).

Registered via ``register_curation_mode(inline_pass_fn=run_deterministic_assembler)``
when ``assembler_deterministic=true``. Returns ``None`` on any failure so the
caller falls through to the full curator loop (graceful degradation).
"""

from __future__ import annotations

import structlog

from archolith_proxy.curator.briefing import PreFetchedFile, SessionBriefing
from archolith_proxy.curator.context_cache import (
    compute_context_signature,
    get_cached_context,
    store_context,
)
from archolith_proxy.curator.state import CuratorSnapshot, cache_snapshot
from archolith_proxy.models.dtos import AssembledContext

logger = structlog.get_logger()

# Rough chars-per-token for budget math (matches the proxy's other estimates).
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _format_file_block(f: PreFetchedFile) -> str:
    """Render one pre-fetched file as the prepper's RELEVANT CODE entry."""
    if f.sections:
        chunks = []
        for start, end, content in f.sections:
            chunks.append(f"{f.path} lines {start}-{end}:\n```\n{content}\n```")
        return "\n".join(chunks)
    if f.outline:
        return f"{f.path} outline:\n{f.outline}"
    return ""


def _truncate_with_closed_fence(text: str, max_chars: int) -> str:
    """Truncate code text to max_chars, closing an open code fence if needed."""
    if max_chars <= 0:
        return ""
    truncated = text[:max_chars]
    if truncated.count("```") % 2 == 1:
        truncated += "\n```"
    return truncated + "\n... [code truncated to fit budget]"


def _truncate_map_to_budget(text: str, max_chars: int) -> str:
    """Keep a map within its explicit character allocation, or omit it."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    marker = "\n... [map truncated by budget]"
    content_chars = max_chars - len(marker)
    if content_chars <= 0:
        return ""
    prefix = text[:content_chars].rsplit("\n", 1)[0].rstrip()
    return f"{prefix}{marker}" if prefix else ""


def build_deterministic_context(
    briefing: SessionBriefing,
    token_budget: int,
    *,
    scored: bool = False,
    topological: bool = False,
    combo: bool = False,
    emit_map: bool = False,
    map_mode: str = "task",
    exemplar_suffixes: tuple[str, ...] = (),
    query: str = "",
    weights: tuple[float, float, float] = (1.0, 1.0, 1.0),
    map_budget_fraction: float = 0.12,
) -> tuple[str, list[dict]]:
    """Compose the context block from the briefing's typed pools, fit to budget.

    Returns ``(context_block, files_selected)``. Pure function — no I/O, no LLM.

    Elastic RELEVANT CODE fill order (precedence: combo > topological > scored > FIFO):
    - ``combo`` True (rung-3 Phase D winner): exemplar-aware blend — guarantee a
      structural exemplar (top-scored file ending in an ``exemplar_suffixes`` marker)
      then interleave scored x topological. Best briefing-only recall.
    - ``topological`` True (Layer 2): dependency in-degree, FOUNDATIONS first, so
      load-bearing files survive truncation.
    - ``scored`` True: generative-agents score order (recency x importance x
      relevance vs ``query``).
    - all False: briefing insertion order (byte-identical to the original fill).

    When ``emit_map`` is True a compact CODE MAP (the MAP job — a structural
    overview) is placed FIRST in the head, so its char cost is subtracted from the
    budget that RELEVANT CODE competes for (the map does not get a free pass). Off
    by default; byte-identical when off. ``map_mode`` selects the map:
    - ``"task"`` (default, the B2b/B2c navigation winner): ``render_task_map`` —
      ranks files by relevance to ``query`` and tags the exemplar, so the agent is
      steered to the template, not to foundations.
    - ``"indegree"`` (legacy): ``render_code_map`` — in-degree foundations + edges.
      B2 found this MISDIRECTS navigation; kept for experiments/comparison only.
    """
    budget_chars = max(0, token_budget) * _CHARS_PER_TOKEN

    # Small, high-value pools — kept verbatim, in canonical section order.
    head_parts: list[str] = []
    if emit_map:
        if map_mode == "indegree":
            from archolith_proxy.curator.dependency_graph import render_code_map
            _map = render_code_map(briefing.files)
        else:
            from archolith_proxy.curator.dependency_graph import render_task_map
            _map = render_task_map(
                briefing.files, query, exemplar_suffixes=exemplar_suffixes,
            )
        if _map:
            # Cap map size using the budget fraction so it never starves RELEVANT CODE
            map_budget_chars = int(budget_chars * map_budget_fraction)
            _map = _truncate_map_to_budget(_map, map_budget_chars)
            if _map:
                head_parts.append(_map)
    if briefing.session_goal:
        head_parts.append(f"=== SESSION GOAL ===\n{briefing.session_goal}")
    if briefing.checkpoint_text:
        head_parts.append(f"=== CURRENT STATE ===\n{briefing.checkpoint_text}")
    if briefing.open_issues_text:
        head_parts.append(f"=== OPEN ISSUES ===\n{briefing.open_issues_text}")
    if briefing.last_verification_text:
        head_parts.append(f"=== LAST VERIFICATION ===\n{briefing.last_verification_text}")
    if briefing.decisions_text:
        head_parts.append(f"=== DECISIONS ===\n{briefing.decisions_text}")
    if briefing.facts_text:
        head_parts.append(f"=== KEY FACTS ===\n{briefing.facts_text}")

    head = "\n\n".join(head_parts)

    # Elastic pool: RELEVANT CODE fills whatever budget remains.
    files_selected: list[dict] = []
    code_blocks: list[str] = []
    remaining = budget_chars - len(head) - len("\n\n=== RELEVANT CODE ===\n")

    if combo:
        from archolith_proxy.curator.dependency_graph import order_by_combo
        if not exemplar_suffixes:
            logger.warning(
                "deterministic_assembler_combo_without_exemplar_suffixes",
                note="combo fill without exemplar_suffixes degenerates to scored+topo interleave",
            )
        ordered_files = order_by_combo(briefing.files, query, exemplar_suffixes)
    elif topological:
        from archolith_proxy.curator.dependency_graph import order_by_topology
        ordered_files = order_by_topology(briefing.files)
    elif scored:
        from archolith_proxy.curator.scoring import score_files
        ordered_files = [f for (_score, f) in score_files(briefing.files, query, weights)]
    else:
        ordered_files = briefing.files

    for f in ordered_files:
        block = _format_file_block(f)
        if not block:
            continue
        cost = len(block) + 1  # +1 for the join newline
        if cost <= remaining:
            code_blocks.append(block)
            files_selected.append({
                "path": f.path,
                "relevance": getattr(f, "relevance", ""),
            })
            remaining -= cost
        elif remaining > 200:
            # Partially include this file's block, then stop.
            code_blocks.append(_truncate_with_closed_fence(block, remaining))
            files_selected.append({
                "path": f.path,
                "relevance": getattr(f, "relevance", ""),
            })
            remaining = 0
            break
        else:
            break

    if code_blocks:
        head = head + "\n\n=== RELEVANT CODE ===\n" + "\n".join(code_blocks)

    return head, files_selected


async def run_deterministic_assembler(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    briefing: SessionBriefing,
    messages: list[dict],
    client,            # unused — kept for inline_pass_fn signature parity
    model: str,        # unused
    settings,
) -> AssembledContext | None:
    """Deterministic inline read — formats the briefing into a context block.

    Same call signature as ``run_assembler`` so it can be registered as the
    inline pass function. Makes NO LLM call. Returns None only if the briefing
    yields no usable content (caller then falls through to the full loop).
    """
    try:
        token_budget = getattr(settings, "assembler_token_budget", 6000)
        scored = bool(getattr(settings, "assembler_scored_selection", False))
        topological = bool(getattr(settings, "assembler_topological_fill", False))
        combo = bool(getattr(settings, "assembler_combo_fill", False))
        emit_map = bool(getattr(settings, "assembler_code_map", False))
        map_mode = getattr(settings, "assembler_code_map_mode", "task") or "task"
        raw_suffixes = getattr(settings, "assembler_exemplar_suffixes", "") or ""
        exemplar_suffixes = tuple(
            s.strip() for s in raw_suffixes.split(",") if s.strip()
        )
        raw_map_budget_fraction = getattr(
            settings, "assembler_code_map_budget_fraction", 0.12,
        )
        map_budget_fraction = (
            0.12 if raw_map_budget_fraction is None else float(raw_map_budget_fraction)
        )

        # === Phase 1: Context Cache Check ===
        context_cache_enabled = bool(getattr(settings, "context_cache_enabled", False))
        cached_result = None

        if context_cache_enabled and session_goal:
            touched_paths = [f.path for f in getattr(briefing, "files", [])]
            signature = compute_context_signature(
                session_goal or "",
                touched_paths,
                user_message or "",
            )

            # Try to get from cache
            db_path = getattr(settings, "curator_state_persist_path", "data/curator_state.db")
            max_age = getattr(settings, "provider_cache_ttl_seconds", 600)

            cached_result = get_cached_context(db_path, session_id, signature, max_age_seconds=max_age)

            if cached_result:
                logger.info(
                    "deterministic_context_cache_hit",
                    session_id=session_id,
                    turn=turn_number,
                    signature=signature[:16],
                )
                # Return cached result
                return AssembledContext(
                    system_message={"role": "system", "content": cached_result["rendered_block"]},
                    graph_context=[{"role": "system", "content": cached_result["rendered_block"]}],
                    coherence_tail=[],
                    token_estimate=_estimate_tokens(cached_result["rendered_block"]),
                    facts_retrieved=0,
                    session_id=session_id,
                    files_selected=cached_result.get("files_selected", []),
                    decisions_selected=[],
                    compression_ratio=1.0,
                    retained_turn_numbers=briefing.retained_turns,
                    curator_tool_log=[],
                )
        context_block, files_selected = build_deterministic_context(
            briefing, token_budget, scored=scored, topological=topological,
            combo=combo, emit_map=emit_map, map_mode=map_mode,
            exemplar_suffixes=exemplar_suffixes,
            query=user_message or "",
            map_budget_fraction=map_budget_fraction,
        )

        if not context_block.strip():
            logger.info(
                "deterministic_assembler_empty_briefing",
                session_id=session_id, turn=turn_number,
            )
            return None

        # === Phase 1: Store in Context Cache (on miss) ===
        if context_cache_enabled and session_goal:
            touched_paths = [f.path for f in getattr(briefing, "files", [])]
            signature = compute_context_signature(
                session_goal or "",
                touched_paths,
                user_message or "",
            )
            db_path = getattr(settings, "curator_state_persist_path", "data/curator_state.db")

            store_context(
                db_path,
                session_id,
                signature,
                context_block,
                files_selected,
                created_turn=turn_number,
                is_cold_start=(turn_number <= 2),
            )
            logger.info(
                "deterministic_context_cache_stored",
                session_id=session_id,
                turn=turn_number,
                signature=signature[:16],
            )

        retained = briefing.retained_turns

        # Cache a snapshot for next turn's delta behaviour, same as run_assembler.
        cache_snapshot(session_id, CuratorSnapshot(
            curated_paths=tuple(sorted(f["path"] for f in files_selected)),
            retained_turn_numbers=tuple(retained) if retained else None,
            context_summary=context_block[:2000],
            tool_calls_used=0,
            turn_number=turn_number,
        ))

        logger.info(
            "deterministic_assembler_complete",
            session_id=session_id, turn=turn_number,
            files=len(files_selected),
            context_len=len(context_block),
            source_turn=briefing.source_turn,
        )

        try:
            from archolith_proxy.metrics import record_metric
            record_metric("deterministic_assemblies", 1)
        except Exception:
            pass

        return AssembledContext(
            system_message={"role": "system", "content": context_block},
            graph_context=[{"role": "system", "content": context_block}],
            coherence_tail=[],
            token_estimate=_estimate_tokens(context_block),
            facts_retrieved=0,
            session_id=session_id,
            files_selected=files_selected,
            decisions_selected=[],
            compression_ratio=1.0,
            retained_turn_numbers=retained,
            curator_tool_log=[],
        )
    except Exception as exc:
        logger.warning(
            "deterministic_assembler_failed",
            session_id=session_id, turn=turn_number, error=str(exc), exc_info=True,
        )
        return None


__all__ = [
    "build_deterministic_context",
    "run_deterministic_assembler",
]
