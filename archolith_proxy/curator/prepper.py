"""Background prepper — dedicated curator for speculative context preparation.

The prepper replaces the single-bot background pass when curation_mode
is "two_curator". It has its own system prompt (focused on anticipating
the next question), its own model config, and access to all curator tools
plus score_file_relevance for smarter file ranking.

Registered via register_curation_mode(background_pass_fn=run_prepper).
"""

from __future__ import annotations

import asyncio
import time

import structlog
from openai import AsyncOpenAI

from archolith_proxy.config import get_settings
from archolith_proxy.curator.briefing import (
    SessionBriefing, build_briefing_from_result,
)
from archolith_proxy.curator.loop import _run_curator_native
from archolith_proxy.curator.schemas import PREPPER_TOOLS
from archolith_proxy.curator.state import get_snapshot
from archolith_proxy.curator.prompts import build_curator_user_prompt

logger = structlog.get_logger()

PREPPER_SYSTEM_PROMPT = """\
You are a context prepper for a coding agent session. Your job is to
speculate about what the coding agent will need next and pre-fetch every
file, fact, and decision that might be relevant.

You have full access to all curator tools plus score_file_relevance.

Your strategy:
1. Start by checking the checkpoint, session goal, and open issues.
2. Score cached files for relevance using score_file_relevance — this tells
   you which files the next question is likely to need. Prioritize files
   with the highest scores.
3. For high-relevance files over 100 lines: call get_file_outline first,
   then get_file_lines for the key sections. For files under 100 lines,
   use get_file directly.
4. Retrieve facts (search_facts / search_facts_semantic) and decisions
   that are likely to be relevant to the next turn. The session's
   trajectory (checkpoint + goal + recent decisions) tells you the
   direction.
5. Use get_open_issues and get_last_verification when open issues exist
   or recent work involved tests/commands.
6. Do NOT call select_relevant_turns — turn selection is the assembler's job.
7. Call tools 8-12 times across all iterations. Be comprehensive —
   this is background compute with no time pressure.
8. Your final response must be the context block, formatted exactly:

=== SESSION GOAL ===
<goal>

=== CURRENT STATE ===
<checkpoint summary and next step>

=== OPEN ISSUES ===
- <issue>

=== LAST VERIFICATION ===
<command and result>

=== RELEVANT CODE ===
<path> lines <start>-<end>:
```
<code>
```

=== KEY FACTS ===
- <fact>

=== DECISIONS ===
- <decision>

Critical output rules:
- Omit any section that has no content. Do NOT write section headers with "None" or "N/A".
- Only include RELEVANT CODE for files you actually retrieved with get_file or get_file_lines.
- Be comprehensive — the goal is to minimize how many tools the inline assembler needs to call.
  Include file outlines even if you only fetched a section, so the assembler knows the full
  structure without calling get_file_outline again.
- Write plain prose context blocks — do NOT emit tool calls, XML tags, or JSON in your final response.
"""


async def run_prepper(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    messages: list[dict],
) -> SessionBriefing | None:
    """Background prepper — produces a SessionBriefing for the assembler.

    Registered via register_curation_mode(background_pass_fn=run_prepper).

    Uses its own model config (prepper_model/base_url/api_key), falls back
    to curator config, then extractor config. Own system prompt (PREPPER_SYSTEM_PROMPT),
    own tool set (PREPPER_TOOLS), and generous iteration budget.

    Returns None on any failure — errors are logged, never raised.
    """
    settings = get_settings()

    # Resolve model: prepper-specific overrides, then curator, then extractor
    model = settings.prepper_model or settings.curator_model or settings.extractor_model
    base_url = settings.prepper_base_url or settings.curator_base_url or settings.extractor_base_url
    api_key = settings.prepper_api_key or settings.curator_api_key or settings.extractor_api_key

    if not api_key:
        logger.warning("prepper_no_api_key", session_id=session_id)
        return None

    # Pre-fetch checkpoint and previous snapshot
    checkpoint = None
    try:
        from archolith_proxy.graph.backend import get_backend, is_graph_ready
        if is_graph_ready():
            checkpoint = await get_backend().get_checkpoint(session_id)
    except Exception:
        pass

    previous_snapshot = get_snapshot(session_id)

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
    budget_s = settings.prepper_latency_budget_ms / 1000

    try:
        result_tuple = await asyncio.wait_for(
            _run_curator_native(
                client=client,
                session_id=session_id,
                user_prompt=user_prompt,
                max_iterations=settings.prepper_max_iterations,
                system_prompt=PREPPER_SYSTEM_PROMPT,
                model=model,
                tool_set=PREPPER_TOOLS,
            ),
            timeout=budget_s,
        )
        result, tool_log, failure = result_tuple
    except asyncio.TimeoutError:
        logger.info("prepper_timeout", session_id=session_id, turn=turn_number,
                     budget_ms=settings.prepper_latency_budget_ms)
        return None
    except Exception as exc:
        logger.warning("prepper_failed", session_id=session_id, turn=turn_number, error=str(exc))
        return None

    if result is None:
        logger.info("prepper_no_result", session_id=session_id, turn=turn_number,
                     failure=failure[:100] if failure else "")
        return None

    latency_ms = (time.monotonic() - t0) * 1000

    # Record the background prepper's curator-model token spend so /metrics
    # reflects the true cost of the two_curator setup. The prepper is a real
    # curator-model call separate from any inline curator pass; counting both
    # in curator_*_tokens_total gives total curator-model spend (no double
    # count — different calls).
    try:
        from archolith_proxy.metrics import record_metric
        record_metric("curator_prompt_tokens_total", getattr(result, "prompt_tokens_used", 0) or 0)
        record_metric("curator_completion_tokens_total", getattr(result, "completion_tokens_used", 0) or 0)
        record_metric("curator_cached_tokens_total", getattr(result, "cached_tokens_used", 0) or 0)
    except Exception:
        pass

    briefing = build_briefing_from_result(
        result=result,
        session_id=session_id,
        turn_number=turn_number,
        latency_ms=latency_ms,
        session_goal=session_goal,
        messages=messages,
        mode="two_curator",
        retained_turns=None,  # prepper does not do turn selection
    )

    logger.info(
        "prepper_complete",
        session_id=session_id, turn=turn_number,
        tool_calls=result.tool_calls_used,
        latency_ms=round(latency_ms, 1),
        files=len(briefing.files),
        context_len=len(result.context_text),
    )

    return briefing


__all__ = ["run_prepper"]
