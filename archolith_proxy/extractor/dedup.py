"""Fact deduplication — skip storing facts that duplicate existing active facts.

Uses Jaccard token overlap on normalized content. A new fact is skipped
if its similarity to any existing active fact exceeds the threshold.

Utility functions (_normalize, _tokenize, jaccard_similarity) are imported
from shared so the graph layer can depend on shared instead of extractor.
"""

from __future__ import annotations

import structlog

from archolith_proxy.shared.text_utils import (
    _normalize,  # noqa: F401 — re-exported for backward compat
    _tokenize,  # noqa: F401 — re-exported for backward compat
    jaccard_similarity,  # noqa: F401 — re-exported for backward compat
)

logger = structlog.get_logger()

# Default similarity threshold — skip if Jaccard > this value
DEFAULT_SIMILARITY_THRESHOLD = 0.85


def is_duplicate(
    new_content: str,
    existing_facts: list[dict],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """Check if a new fact duplicates any existing active fact.

    Args:
        new_content: The candidate fact content.
        existing_facts: List of existing fact dicts (each with a "content" key).
        threshold: Jaccard similarity threshold above which facts are considered duplicates.

    Returns:
        True if the new fact is a duplicate of an existing fact.
    """
    for existing in existing_facts:
        existing_content = existing.get("content", "")
        if not existing_content:
            continue
        sim = jaccard_similarity(new_content, existing_content)
        if sim > threshold:
            logger.debug(
                "fact_dedup_skip",
                new=new_content[:60],
                existing=existing_content[:60],
                similarity=round(sim, 3),
            )
            return True
    return False


def deduplicate_facts(
    new_facts: list[dict],
    existing_facts: list[dict],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[dict]:
    """Filter a list of new facts, removing any that duplicate existing facts.

    Args:
        new_facts: Candidate facts to store.
        existing_facts: Already-stored active facts.
        threshold: Jaccard similarity threshold.

    Returns:
        Filtered list of new facts with duplicates removed.
    """
    kept = []
    skipped = 0
    for fact in new_facts:
        content = fact.get("content", "")
        if is_duplicate(content, existing_facts, threshold):
            skipped += 1
        else:
            kept.append(fact)
    if skipped > 0:
        logger.info(
            "facts_deduplicated",
            new_count=len(new_facts),
            kept=len(kept),
            skipped=skipped,
        )
    return kept
