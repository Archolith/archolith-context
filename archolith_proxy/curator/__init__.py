"""Curator — LLM-driven context manager for coding agent sessions.

The curator replaces the heuristic fact-scoring assembler with an active,
tool-calling context manager LLM. It receives the session goal and the
current single-turn question, calls tools to retrieve relevant file sections
from the local content cache and facts from the graph, and returns a
structured context block.

Entry point: curate_context() — wired in chat.py as the primary assembly
path, with the heuristic assembler as fallback.
"""

from __future__ import annotations

import asyncio

import structlog
from openai import AsyncOpenAI

from archolith_proxy.config import get_settings
from archolith_proxy.curator.loop import _run_curator_native, _run_curator_nous
from archolith_proxy.curator.prompts import CURATOR_SYSTEM_PROMPT, build_curator_user_prompt
from archolith_proxy.curator.result import CuratorResult
from archolith_proxy.models.dtos import AssembledContext

logger = structlog.get_logger()


async def curate_context(
    session_id: str,
    turn_number: int,
    user_message: str,
    session_goal: str | None,
    http_client,
    messages: list[dict],
) -> AssembledContext | None:
    """Run the curator LLM to build a context block for the coding agent.

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

    # Build prompt
    user_prompt = build_curator_user_prompt(session_goal, user_message)

    # Build OpenAI client
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    try:
        # Apply latency budget
        result: CuratorResult | None = await asyncio.wait_for(
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
    except asyncio.TimeoutError:
        logger.info("curator_timeout", session_id=session_id, turn=turn_number)
        from archolith_proxy.metrics import record_metric
        record_metric("curator_timeouts", 1)
        return None
    except Exception:
        logger.warning("curator_failed", session_id=session_id, exc_info=True)
        return None

    if result is None:
        logger.info("curator_fallback", session_id=session_id, turn=turn_number)
        from archolith_proxy.metrics import record_metric
        record_metric("curator_fallbacks", 1)
        return None

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
    )

