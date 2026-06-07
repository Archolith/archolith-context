"""Fact deduplication — skip storing facts that duplicate existing active facts.

Uses Jaccard token overlap on normalized content. A new fact is skipped
if its similarity to any existing active fact exceeds the threshold.

Utility functions (_normalize, _tokenize, jaccard_similarity) are imported
from shared so the graph layer can depend on shared instead of extractor.
"""

from __future__ import annotations

import structlog

from archolith_proxy.shared.text_utils import (
    _normalize,
    _tokenize,
    jaccard_similarity,
)

logger = structlog.get_logger()

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "is_duplicate",
    "deduplicate_facts",
    # Re-exported from shared.text_utils for backward compat
    "_normalize",
    "_tokenize",
    "jaccard_similarity",
]

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

    Also deduplicates within the batch: if two new facts are near-duplicates
    of each other, only the first is kept.

    Args:
        new_facts: Candidate facts to store.
        existing_facts: Already-stored active facts.
        threshold: Jaccard similarity threshold.

    Returns:
        Filtered list of new facts with duplicates (internal and external) removed.
    """
    # First pass: dedup within the new_facts batch
    batch_kept = []
    batch_skipped = 0
    for fact in new_facts:
        content = fact.get("content", "")
        # Check if this fact duplicates any fact already in batch_kept
        if is_duplicate(content, batch_kept, threshold):
            batch_skipped += 1
        else:
            batch_kept.append(fact)

    if batch_skipped > 0:
        logger.debug(
            "facts_within_batch_dedup",
            input_count=len(new_facts),
            kept=len(batch_kept),
            skipped=batch_skipped,
        )

    # Second pass: dedup batch_kept against existing_facts
    kept = []
    external_skipped = 0
    for fact in batch_kept:
        content = fact.get("content", "")
        if is_duplicate(content, existing_facts, threshold):
            external_skipped += 1
        else:
            kept.append(fact)

    total_skipped = batch_skipped + external_skipped
    if total_skipped > 0:
        logger.info(
            "facts_deduplicated",
            new_count=len(new_facts),
            kept=len(kept),
            skipped=total_skipped,
            within_batch=batch_skipped,
            vs_existing=external_skipped,
        )
    return kept
