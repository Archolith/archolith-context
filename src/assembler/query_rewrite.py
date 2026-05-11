"""Query rewriting for ambiguous user messages.

When a user message contains pronouns, references, or vague directives
("do it", "continue", "fix that"), embedding the raw text produces a
generic vector that won't match specific session facts. This module
detects such messages and rewrites them to be self-contained by
resolving references against recent conversation context.

Inspired by ogcode's rewriteQuery() approach — resolve pronouns and
references before embedding so the query vector captures the user's
actual intent rather than the vague surface text.

Gated behind QUERY_REWRITE_ENABLED=true (default false).
Cost: ~$0.0003/turn when triggered (only fires for ambiguous queries).
"""

from __future__ import annotations

import re

import httpx
import structlog

from src.config import get_settings

logger = structlog.get_logger()

# Patterns that indicate a query needs rewriting — pronouns, vague
# directives, and deictic references that depend on prior context.
_AMBIGUOUS_PATTERNS = [
    # Pronouns
    re.compile(r'\b(it|they|them|this|that|these|those|he|she|we)\b', re.IGNORECASE),
    # Vague directives
    re.compile(r'\b(do it|continue|fix (?:that|it|the)|try again|go ahead|proceed)\b', re.IGNORECASE),
    # Deictic references
    re.compile(r'\b(the (?:previous|above|last|earlier|former)\s+\w+)\b', re.IGNORECASE),
    # Short queries (< 5 words) are often context-dependent
    re.compile(r'^\s*\w+(?:\s+\w+){0,3}\s*[.!?]?\s*$'),
]

# Words that indicate a short query is specific enough to not need rewriting
_SPECIFIC_KEYWORDS = {
    "file", "class", "function", "method", "variable", "module", "import",
    "test", "error", "bug", "exception", "traceback", "feature", "api",
    "database", "query", "endpoint", "route", "model", "entity", "table",
    "column", "index", "service", "component", "page", "build", "deploy",
    "docker", "container", "git", "commit", "branch", "merge", "pull",
    "add", "create", "delete", "remove", "update", "rename", "move",
    "refactor", "implement", "configure", "install", "debug", "log",
}


def needs_rewrite(query: str) -> bool:
    """Check if a user message needs rewriting before embedding.

    Returns True if the query contains pronouns, vague directives,
    or is too short to be self-contained.

    Priority: pronouns and deictic references ALWAYS need rewriting —
    they are inherently ambiguous without context. The keyword override
    only applies to vague directives and short queries that happen to
    contain specific technical terms.
    """
    if not query or not query.strip():
        return False

    text = query.strip()

    # Pattern 0 (pronouns) and Pattern 2 (deictic references) are ALWAYS
    # ambiguous — they can't be resolved without context, regardless of
    # other keywords present. "Update this method" still needs to know
    # WHICH method "this" refers to.
    if _AMBIGUOUS_PATTERNS[0].search(text):  # pronouns
        return True
    if _AMBIGUOUS_PATTERNS[2].search(text):  # deictic references
        return True

    # Vague directives and short queries may be specific enough on their own
    # if they contain technical keywords
    for pattern in _AMBIGUOUS_PATTERNS[1:]:  # skip pronouns and deictics (already checked)
        if pattern is _AMBIGUOUS_PATTERNS[0] or pattern is _AMBIGUOUS_PATTERNS[2]:
            continue  # already checked above
        if pattern.search(text):
            words = set(text.lower().split())
            # Short queries with specific technical keywords are likely self-contained
            if words & _SPECIFIC_KEYWORDS and len(words) >= 3:
                continue
            return True

    return False


async def rewrite_query(
    http_client: httpx.AsyncClient,
    query: str,
    recent_exchanges: list[dict],
) -> str | None:
    """Rewrite an ambiguous query to be self-contained.

    Uses the extractor model (gpt-4.1-mini) to resolve pronouns and
    references against recent conversation context.

    Args:
        http_client: HTTP client for the extractor API call.
        query: The ambiguous user message to rewrite.
        recent_exchanges: Recent user/assistant message pairs to
            use as context for resolving references.

    Returns:
        Rewritten query string, or None if rewriting fails.
    """
    settings = get_settings()

    if not settings.extractor_api_key:
        logger.warning("query_rewrite_skipped_no_key")
        return None

    # Build context from recent exchanges
    context_parts = []
    for msg in recent_exchanges[-6:]:  # Last 3 exchanges (6 messages max)
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            # Truncate long messages to keep the prompt small
            context_parts.append(f"{role}: {content[:500]}")

    context_text = "\n".join(context_parts) if context_parts else "(no recent context)"

    prompt = f"""Resolve ALL references in the query to make it self-contained. Replace pronouns (it, they, this, that) with the specific noun they refer to. Expand vague directives (do it, continue, fix that) into specific actions based on context.

Recent conversation:
{context_text}

Query: {query}

Respond ONLY with the rewritten query. No explanation, no quotes, no prefix."""

    payload = {
        "model": settings.extractor_model,
        "messages": [
            {"role": "system", "content": "You resolve references in short queries. Output only the rewritten query with all pronouns and references resolved. Be concise and specific."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
    }

    try:
        resp = await http_client.post(
            f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.extractor_api_key}",
                "Content-Type": "application/json",
            },
            content=__import__("json").dumps(payload).encode(),
        )
        resp.raise_for_status()
        data = resp.json()

        rewritten = data["choices"][0]["message"]["content"].strip()

        # Validate: rewritten query should be non-empty and different from original
        if not rewritten or rewritten.lower() == query.lower():
            logger.debug("query_rewrite_no_change", original=query, rewritten=rewritten)
            return None

        logger.info(
            "query_rewritten",
            original=query[:80],
            rewritten=rewritten[:80],
        )
        return rewritten

    except Exception as e:
        logger.warning("query_rewrite_failed", error=str(e))
        return None


def extract_recent_exchanges(messages: list[dict], max_exchanges: int = 3) -> list[dict]:
    """Extract recent user/assistant exchanges from the message array.

    Returns the last N user+assistant message pairs (2*N messages max),
    suitable for use as context in query rewriting.

    Args:
        messages: The full message array from the request.
        max_exchanges: Maximum number of exchange pairs to return.

    Returns:
        List of recent messages (user/assistant only, last max_exchanges pairs).
    """
    # Walk backward from the end, collecting user/assistant messages
    recent = []
    exchange_count = 0

    for msg in reversed(messages):
        role = msg.get("role", "")
        if role in ("user", "assistant"):
            recent.append(msg)
            if role == "user":
                exchange_count += 1
                if exchange_count >= max_exchanges:
                    break
        # Skip system, tool messages — not useful for reference resolution

    # Reverse to get chronological order
    recent.reverse()
    return recent
