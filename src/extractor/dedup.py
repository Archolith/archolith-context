"""Fact deduplication — skip storing facts that duplicate existing active facts.

Uses Jaccard token overlap on normalized content. A new fact is skipped
if its similarity to any existing active fact exceeds the threshold.
"""

from __future__ import annotations

import re
import structlog

logger = structlog.get_logger()

# Default similarity threshold — skip if Jaccard > this value
DEFAULT_SIMILARITY_THRESHOLD = 0.85


def _normalize(text: str) -> str:
    """Normalize fact content for comparison: lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    # Remove surrounding quotes
    text = text.strip("\"'")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Strip trailing punctuation
    text = re.sub(r"[.!?;:]+$", "", text)
    return text


def _tokenize(text: str) -> set[str]:
    """Split normalized text into a set of word tokens."""
    return set(_normalize(text).split())


def jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two fact strings.

    Returns a value between 0.0 (no overlap) and 1.0 (identical token sets).
    """
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


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
