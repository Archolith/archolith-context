"""Inline assembler — fast context formatter from pre-built briefing.

The assembler replaces the single-bot inline pass when curation_mode
is "two_curator". It receives a pre-built SessionBriefing from the prepper
and formats the context block with a minimal tool set (select_relevant_turns
+ get_file_lines) and a tight iteration budget (1-2).

Registered via register_curation_mode(inline_pass_fn=run_assembler).
"""

from __future__ import annotations

import asyncio

import structlog
from openai import AsyncOpenAI

from archolith_proxy.config import get_settings  # noqa: F401 - used in tests for mocking
from archolith_proxy.curator.briefing import SessionBriefing, format_briefing_for_prompt
from archolith_proxy.curator.loop import _run_curator_native
from archolith_proxy.curator.prompts import build_curator_user_prompt
from archolith_proxy.curator.schemas import ASSEMBLER_TOOLS
from archolith_proxy.curator.state import (
    CuratorSnapshot, cache_snapshot, get_snapshot,
)
from archolith_proxy.models.dtos import AssembledContext

logger = structlog.get_logger()

ASSEMBLER_SYSTEM_PROMPT = """\
You are a context assembler for a coding agent session. You receive a
pre-built briefing from the prepper that already fetched files, facts,
decisions, and session state.

Your job is to:
1. Read the briefing (shown as "Previous curator context" in the user prompt).
2. Match it against the current question.
3. Select which historical turns to retain via select_relevant_turns.
4. Optionally call get_file_lines for one-off file sections the prepper
   missed or that the current question needs beyond what was pre-fetched.
5. Emit the final context block.

Rules:
- DO NOT re-fetch what is already in the briefing. The prepper already did
  that work. Only call get_file_lines for NEW files or sections the current
  question needs that the briefing does not cover.
- DO NOT call search_facts, get_checkpoint, get_open_issues, or other
  broad tools — those were already called by the prepper. The briefing
  contains everything from those calls.
- Call select_relevant_turns if the turn inventory shows middle turns
  worth retaining. Order by relevance to the current question.
- You have at most 2 iterations. Most of the time you should do 1 tool call
  (select_relevant_turns) and then emit the context block.
- If the briefing fully covers the current question, just call
  select_relevant_turns (if needed) and then emit.

Your final response IS the context block. Format it exactly as:

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
- Include any file sections from the briefing that are still relevant to the current question.
  The prepper may have fetched more than needed — select only what matches the current question.
- Adapt the briefing's RELEVANT CODE to the current question. If the current question is about
  a different part of the same file, call get_file_lines to get the new section.
- Write plain prose context blocks — do NOT emit tool calls, XML tags, or JSON in your final response.
"""


async def run_assembler(
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
    """Fast inline assembler — reads briefing, formats context block.

    Registered via register_curation_mode(inline_pass_fn=run_assembler).

    Resolves its own model config (assembler_model/base_url/api_key), falls
    back to curator config, then extractor config. Uses ASSEMBLER_SYSTEM_PROMPT
    and ASSEMBLER_TOOLS (minimal: select_relevant_turns + get_file_lines).

    Returns None on failure so the caller falls through to the full curator loop.
    """
    # Resolve model: assembler-specific overrides, then curator, then extractor
    asm_model = settings.assembler_model or settings.curator_model or settings.extractor_model
    asm_base_url = settings.assembler_base_url or settings.curator_base_url or settings.extractor_base_url
    asm_api_key = settings.assembler_api_key or settings.curator_api_key or settings.extractor_api_key

    if not asm_api_key:
        logger.warning("assembler_no_api_key", session_id=session_id)
        return None

    # Create assembler-specific client if the model differs from the caller's model
    if asm_base_url != client._base_url or asm_api_key != client.api_key:
        client = AsyncOpenAI(base_url=asm_base_url, api_key=asm_api_key)

    # Format briefing for assembler prompt
    briefing_text = format_briefing_for_prompt(briefing)

    # Build the base user prompt (with checkpoint, snapshot, turn inventory)
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

    inline_latency_budget = min(settings.assembler_latency_budget_ms, 3000)

    try:
        result_tuple = await asyncio.wait_for(
            _run_curator_native(
                client=client,
                session_id=session_id,
                user_prompt=user_prompt,
                max_iterations=settings.assembler_max_iterations,
                system_prompt=ASSEMBLER_SYSTEM_PROMPT,
                model=asm_model,
                tool_set=ASSEMBLER_TOOLS,
            ),
            timeout=inline_latency_budget / 1000,
        )
        result, tool_log, failure = result_tuple
    except asyncio.TimeoutError:
        logger.info("assembler_timeout", session_id=session_id, turn=turn_number)
        return None
    except Exception as exc:
        logger.warning("assembler_failed", session_id=session_id, turn=turn_number, error=str(exc))
        return None

    if result is None:
        logger.info("assembler_no_result", session_id=session_id, turn=turn_number,
                     failure=failure[:100] if failure else "")
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

    logger.info(
        "assembler_complete",
        session_id=session_id, turn=turn_number,
        tool_calls=result.tool_calls_used,
        context_len=len(result.context_text),
    )

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


__all__ = ["run_assembler"]
