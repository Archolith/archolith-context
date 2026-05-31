"""Curator — LLM-driven context manager for coding agent sessions.

The curator replaces the heuristic fact-scoring assembler with an active,
tool-calling context manager LLM. It receives the session goal and the
current single-turn question, calls tools to retrieve relevant file sections
from the local content cache and facts from the graph, and returns a
structured context block.

Entry point: curate_context() — wired in chat.py as the primary assembly
path, with the heuristic assembler as fallback.

Two-pass mode: when background_pass_enabled is True, the curator runs twice:
  1. Background pass (after response completes): same bot, 12 iterations,
     writes a SessionBriefing to cache.
  2. Inline pass (on next request): reads briefing, formats as prompt text,
     runs curator with 2 iterations (most work already done).
"""

from __future__ import annotations

import asyncio
import time

import structlog
from openai import AsyncOpenAI

from archolith_proxy.config import get_settings
from archolith_proxy.curator.briefing import SessionBriefing, format_briefing_for_prompt
from archolith_proxy.curator.loop import _run_curator_native, _run_curator_nous
from archolith_proxy.curator.prompts import CURATOR_SYSTEM_PROMPT, build_curator_user_prompt
from archolith_proxy.curator.result import CuratorResult
from archolith_proxy.curator.state import (
    CuratorSnapshot, cache_snapshot, get_snapshot,
    cache_briefing, get_briefing, is_briefing_fresh,
)
from archolith_proxy.models.dtos import AssembledContext

logger = structlog.get_logger()

# Side-channel for curator failure data — lets chat.py show what the curator
# tried before falling through to passthrough. Keyed by session_id, consumed
# once via get_last_attempt().
_last_attempt: dict[str, dict] = {}


def get_last_attempt(session_id: str) -> dict | None:
    """Pop the last curator attempt info for a session.

    Returns {"tool_log": [...], "failure_reason": "..."} or None.
    Consumed once — subsequent calls return None until the next attempt.
    """
    return _last_attempt.pop(session_id, None)


# ---------------------------------------------------------------------------
# Background pass — runs after response completes, no time limit
# ---------------------------------------------------------------------------

async def run_background_pass(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    messages: list[dict],
) -> None:
    """Run the background curator pass after a response completes.

    Same curator bot, same prompt, same tools — but with a generous
    iteration budget (12) and no latency timeout. Writes a SessionBriefing
    to the in-memory cache for the next inline pass to consume.

    Never raises — errors are logged and the briefing is simply not written.
    """
    settings = get_settings()

    if not settings.background_pass_enabled or not settings.curator_enabled:
        return

    # Debounce: wait for extraction to finish so the graph has fresh data
    debounce_s = settings.background_pass_debounce_ms / 1000
    await asyncio.sleep(debounce_s)

    # Resolve model/url/key
    model = settings.curator_model or settings.extractor_model
    base_url = settings.curator_base_url or settings.extractor_base_url
    api_key = settings.curator_api_key or settings.extractor_api_key

    if not api_key:
        return

    # Pre-fetch checkpoint
    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass

    # Retrieve previous snapshot for delta behaviour
    previous_snapshot = get_snapshot(session_id)

    # Build prompt — same as inline pass
    user_prompt = build_curator_user_prompt(
        session_goal,
        user_message,
        messages=messages,
        coherence_tail_size=settings.coherence_tail_size,
        max_tail_messages=settings.max_tail_messages,
        checkpoint=checkpoint,
        previous_snapshot=previous_snapshot,
    )

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    t0 = time.monotonic()
    try:
        result, _bg_tool_log, _bg_failure = await _run_curator_native(
            client=client,
            session_id=session_id,
            user_prompt=user_prompt,
            max_iterations=settings.background_pass_max_iterations,
            system_prompt=CURATOR_SYSTEM_PROMPT,
            model=model,
        )
    except Exception:
        logger.warning("background_pass_failed", session_id=session_id, turn=turn_number, exc_info=True)
        return

    latency_ms = (time.monotonic() - t0) * 1000

    if result is None:
        logger.info("background_pass_no_result", session_id=session_id, turn=turn_number)
        return

    # Build SessionBriefing from the curator result
    briefing = _build_briefing_from_result(
        result=result,
        session_id=session_id,
        turn_number=turn_number,
        latency_ms=latency_ms,
        session_goal=session_goal,
        messages=messages,
    )

    cache_briefing(session_id, briefing)

    try:
        from archolith_proxy.metrics import record_metric
        record_metric("background_pass_successes", 1)
    except Exception:
        pass

    logger.info(
        "background_pass_complete",
        session_id=session_id,
        turn=turn_number,
        tool_calls=result.tool_calls_used,
        latency_ms=round(latency_ms, 1),
        files=len(briefing.files),
        context_len=len(result.context_text),
    )


def _build_briefing_from_result(
    result: CuratorResult,
    session_id: str,
    turn_number: int,
    latency_ms: float,
    session_goal: str | None,
    messages: list[dict],
) -> SessionBriefing:
    """Parse a CuratorResult into a SessionBriefing for the inline pass.

    The background pass already did the expensive work (file fetching,
    fact retrieval, turn selection). We capture it all in a data snapshot.
    """
    # Extract per-file sections from the tool log
    files: dict[str, list[tuple[int, int, str]]] = {}
    file_outlines: dict[str, str] = {}
    file_relevance: dict[str, str] = {}

    for tc in result.tool_log:
        if tc.tool in ("get_file", "get_file_lines") and tc.status == "ok":
            path = tc.args.get("path", "")
            if not path:
                continue
            if path not in files:
                files[path] = []
            # Capture the result preview as the "section content"
            content = tc.result_preview or ""
            start = tc.args.get("start_line", tc.args.get("offset", 0))
            end = tc.args.get("end_line", tc.args.get("limit", 0))
            if isinstance(start, int) and isinstance(end, int) and end > start:
                files[path].append((start, end, content))
            else:
                files[path].append((0, 0, content))
        elif tc.tool == "get_file_outline" and tc.status == "ok":
            path = tc.args.get("path", "")
            if path:
                file_outlines[path] = tc.result_preview or ""

    from archolith_proxy.curator.briefing import PreFetchedFile
    prefetched = []
    for path, sections in files.items():
        prefetched.append(PreFetchedFile(
            path=path,
            outline=file_outlines.get(path, ""),
            sections=sections,
            relevance=file_relevance.get(path, "retrieved by background pass"),
        ))

    # Parse the context block for session state sections
    context_text = result.context_text
    checkpoint_text = _extract_section(context_text, "CURRENT STATE")
    open_issues_text = _extract_section(context_text, "OPEN ISSUES")
    last_verification_text = _extract_section(context_text, "LAST VERIFICATION")
    facts_text = _extract_section(context_text, "KEY FACTS")
    decisions_text = _extract_section(context_text, "DECISIONS")

    return SessionBriefing(
        session_id=session_id,
        source_turn=turn_number,
        timestamp=time.time(),
        checkpoint_text=checkpoint_text,
        open_issues_text=open_issues_text,
        last_verification_text=last_verification_text,
        decisions_text=decisions_text,
        session_goal=session_goal or "",
        facts_text=facts_text,
        files=prefetched,
        retained_turns=result.retained_turn_numbers,
        context_block=context_text,
        tool_calls_used=result.tool_calls_used,
        iterations_used=result.tool_calls_used,  # approximation
        latency_ms=latency_ms,
    )


def _extract_section(context_text: str, section_name: str) -> str:
    """Extract a named section from the curator's context block.

    Sections are delimited by === SECTION_NAME === headers.
    Returns empty string if the section is not found.
    """
    import re
    # Match any section header (=== ONE OR MORE WORDS ===) as delimiter
    pattern = rf"=== {section_name} ===\s*\n(.*?)(?=\n=== .+? ===|$)"
    match = re.search(pattern, context_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# Inline pass — reads briefing, runs curator with minimal iterations
# ---------------------------------------------------------------------------

async def _run_with_briefing(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    briefing: SessionBriefing,
    messages: list[dict],
    client: AsyncOpenAI,
    model: str,
    settings,
) -> AssembledContext | None:
    """Inline pass with pre-built briefing injection.

    Injects the briefing into the user prompt so the curator sees its own
    prior output. Runs with 2 iterations — enough to adjust or re-emit.
    """
    # Build the briefing prompt section
    briefing_text = format_briefing_for_prompt(briefing)

    # Build the normal user prompt (without briefing)
    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass

    previous_snapshot = get_snapshot(session_id)

    base_prompt = build_curator_user_prompt(
        session_goal,
        user_message,
        messages=messages,
        coherence_tail_size=settings.coherence_tail_size,
        max_tail_messages=settings.max_tail_messages,
        checkpoint=checkpoint,
        previous_snapshot=previous_snapshot,
    )

    # Prepend briefing to the user prompt
    user_prompt = briefing_text + "\n\n---\n\n" + base_prompt

    # Inline briefing pass: tight budget, 2 iterations
    inline_latency_budget = min(settings.curator_latency_budget_ms, 3000)
    try:
        result_tuple = await asyncio.wait_for(
            _run_curator_native(
                client=client,
                session_id=session_id,
                user_prompt=user_prompt,
                max_iterations=2,
                system_prompt=CURATOR_SYSTEM_PROMPT,
                model=model,
            ),
            timeout=inline_latency_budget / 1000,
        )
        result, _inl_tool_log, _inl_failure = result_tuple
    except asyncio.TimeoutError:
        logger.info("inline_briefing_timeout", session_id=session_id, turn=turn_number)
        return None
    except Exception:
        logger.warning("inline_briefing_failed", session_id=session_id, exc_info=True)
        return None

    if result is None:
        logger.info("inline_briefing_no_result", session_id=session_id, turn=turn_number)
        return None

    # Cache snapshot for next turn's delta behaviour
    _max_summary = 2000
    cache_snapshot(session_id, CuratorSnapshot(
        curated_paths=tuple(sorted(result.curated_paths)),
        retained_turn_numbers=tuple(result.retained_turn_numbers) if result.retained_turn_numbers else None,
        context_summary=result.context_text[:_max_summary],
        tool_calls_used=result.tool_calls_used,
        turn_number=turn_number,
    ))

    return AssembledContext(
        system_message={"role": "system", "content": result.context_text},
        graph_context=[{"role": "system", "content": result.context_text}],
        coherence_tail=[],
        token_estimate=result.estimated_tokens,
        facts_retrieved=result.tool_calls_used,
        session_id=session_id,
        files_selected=[{"path": p} for p in result.curated_paths],
        decisions_selected=[],
        compression_ratio=1.0,
        retained_turn_numbers=result.retained_turn_numbers,
        curator_tool_log=[tc.to_dict() for tc in result.tool_log],
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def curate_context(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    http_client,
    messages: list[dict],
) -> AssembledContext | None:
    """Run the curator LLM to build a context block for the coding agent.

    Two-pass dispatch:
    1. If a fresh briefing exists → inline pass (1-2 iterations, <1.5s)
    2. If a stale briefing exists → inline pass (still useful as delta)
    3. No briefing → full curator loop (current behavior, 4-6 iterations)

    Returns AssembledContext on success, None on timeout/fallback/disabled.
    Falls back gracefully — never raises to the caller.
    """
    settings = get_settings()

    # Gate: curator must be enabled and file cache must be on
    if not settings.curator_enabled or not settings.file_cache_enabled:
        return None

    # Cold-start gate: don't curate until enough turns
    user_turns = sum(1 for m in messages if m.get("role") == "user")
    if user_turns < settings.cold_start_turns:
        return None

    # Resolve model/url/key: curator-specific overrides, fall back to extractor
    model = settings.curator_model or settings.extractor_model
    base_url = settings.curator_base_url or settings.extractor_base_url
    api_key = settings.curator_api_key or settings.extractor_api_key

    if not api_key:
        logger.warning("curator_no_api_key", session_id=session_id)
        return None

    # Build OpenAI client
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    # --- Two-pass dispatch ---
    briefing = get_briefing(session_id)
    fresh = is_briefing_fresh(session_id, turn_number)

    if briefing and (fresh or briefing.source_turn >= turn_number - 2):
        # Briefing-assisted inline pass (fresh or stale-by-1)
        assembly_tag = "briefing" if fresh else "briefing_stale"
        logger.info(
            "curator_briefing_pass",
            session_id=session_id,
            turn=turn_number,
            briefing_source=briefing.source_turn,
            fresh=fresh,
            tag=assembly_tag,
        )
        result = await _run_with_briefing(
            session_id=session_id,
            turn_number=turn_number,
            user_message=user_message,
            session_goal=session_goal,
            briefing=briefing,
            messages=messages,
            client=client,
            model=model,
            settings=settings,
        )
        if result is not None:
            return result
        # Briefing pass failed — fall through to full curator
        logger.info("curator_briefing_fallback", session_id=session_id, turn=turn_number)

    # --- Full curator loop (current behavior) ---
    # Pre-fetch checkpoint so the curator can skip the get_checkpoint tool call.
    # This saves one full LLM iteration (~1-2s) on every curator run.
    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass  # Non-fatal — curator falls back to calling get_checkpoint itself

    # Retrieve previous curator snapshot for delta behaviour
    previous_snapshot = get_snapshot(session_id)

    # Build prompt — include turn inventory so curator can call select_relevant_turns
    user_prompt = build_curator_user_prompt(
        session_goal,
        user_message,
        messages=messages,
        coherence_tail_size=settings.coherence_tail_size,
        max_tail_messages=settings.max_tail_messages,
        checkpoint=checkpoint,
        previous_snapshot=previous_snapshot,
    )

    attempt_tool_log: list = []
    attempt_failure: str = ""
    try:
        # Apply latency budget
        result_tuple = await asyncio.wait_for(
            _run_curator_native(
                client=client,
                session_id=session_id,
                user_prompt=user_prompt,
                max_iterations=settings.curator_max_iterations,
                system_prompt=CURATOR_SYSTEM_PROMPT,
                model=model,
            ),
            timeout=settings.curator_latency_budget_ms / 1000,
        )
        result, attempt_tool_log, attempt_failure = result_tuple
    except asyncio.TimeoutError:
        logger.info("curator_timeout", session_id=session_id, turn=turn_number)
        from archolith_proxy.metrics import record_metric
        record_metric("curator_timeouts", 1)
        attempt_failure = "timeout"
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in attempt_tool_log],
            "failure_reason": attempt_failure,
        }
        return None
    except Exception as exc:
        logger.warning("curator_failed", session_id=session_id, exc_info=True)
        attempt_failure = f"exception: {str(exc)[:200]}"
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in attempt_tool_log],
            "failure_reason": attempt_failure,
        }
        return None

    if result is None:
        logger.info("curator_fallback", session_id=session_id, turn=turn_number)
        from archolith_proxy.metrics import record_metric
        record_metric("curator_fallbacks", 1)
        _last_attempt[session_id] = {
            "tool_log": [tc.to_dict() for tc in attempt_tool_log],
            "failure_reason": attempt_failure,
        }
        return None

    # Cache snapshot for next turn's delta behaviour
    _max_summary = 2000
    cache_snapshot(session_id, CuratorSnapshot(
        curated_paths=tuple(sorted(result.curated_paths)),
        retained_turn_numbers=tuple(result.retained_turn_numbers) if result.retained_turn_numbers else None,
        context_summary=result.context_text[:_max_summary],
        tool_calls_used=result.tool_calls_used,
        turn_number=turn_number,
    ))

    # Map CuratorResult to AssembledContext
    return AssembledContext(
        system_message={"role": "system", "content": result.context_text},
        graph_context=[{"role": "system", "content": result.context_text}],
        coherence_tail=[],
        token_estimate=result.estimated_tokens,
        facts_retrieved=result.tool_calls_used,
        session_id=session_id,
        files_selected=[{"path": p} for p in result.curated_paths],
        decisions_selected=[],
        compression_ratio=1.0,
        retained_turn_numbers=result.retained_turn_numbers,
        curator_tool_log=[tc.to_dict() for tc in result.tool_log],
    )

