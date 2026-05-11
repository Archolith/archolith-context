"""Context-overflow compaction via LLM summarization.

When the assembled context + coherence tail still exceeds the token budget,
this module uses the extractor model to summarize the context block into a
shorter version, preserving key facts while dropping verbose observations.

Gated behind COMPACTION_ENABLED=true (default false).
Cost: ~$0.001/turn when triggered (should be rare).
"""

from __future__ import annotations

import json

import structlog

from src.config import get_settings

logger = structlog.get_logger()

_COMPACTION_SYSTEM_PROMPT = """You are a context compactor. Your job is to summarize a session's knowledge graph context into a shorter version while preserving critical information.

Preserve: errors, decisions, file paths, current goals, facts with high confidence.
Drop: resolved issues, verbose observations, redundant facts, low-confidence observations.
Keep the two-tier structure (SESSION OVERVIEW and RELEVANT CONTEXT).
Respond with ONLY the compacted context, no explanation."""


async def compact_context(
    http_client,
    context_block: str,
    target_tokens: int,
) -> str | None:
    """Summarize a context block to fit within target token budget.

    Uses the extractor model (gpt-4.1-mini) to produce a shorter version.
    Returns None if compaction fails (caller falls back to passthrough).

    Args:
        http_client: httpx.AsyncClient for API calls.
        context_block: The full context block text to compact.
        target_tokens: Approximate target token count for the result.

    Returns:
        Compacted context string, or None on failure.
    """
    settings = get_settings()

    if not settings.embedding_api_key:
        logger.warning("compaction_skipped_no_api_key")
        return None

    prompt = (
        f"Summarize the following session context into at most {target_tokens} tokens.\n"
        f"Preserve: errors, decisions, file paths, current goals.\n"
        f"Drop: resolved issues, verbose observations, redundant facts.\n\n"
        f"{context_block}\n\n"
        f"Respond with ONLY the compacted context, no explanation."
    )

    try:
        payload = {
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": min(target_tokens * 2, 4000),
        }

        resp = await http_client.post(
            f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.extractor_api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload).encode(),
        )
        resp.raise_for_status()
        data = resp.json()
        result = data["choices"][0]["message"]["content"]

        if result and result.strip():
            logger.info(
                "context_compacted",
                original_chars=len(context_block),
                compacted_chars=len(result),
                target_tokens=target_tokens,
            )
            return result.strip()
        return None

    except Exception as e:
        logger.warning("compaction_failed", error=str(e))
        return None
