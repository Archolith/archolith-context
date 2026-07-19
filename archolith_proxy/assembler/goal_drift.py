"""Goal Drift Detection (Phase 0).

Lightweight detection of when a session has drifted from its original goal.
This module provides detection only — no re-weighting is applied yet.

For Phase 0 we use a simple lexical (token overlap) similarity.
This will later be upgraded to embedding-based cosine similarity.
"""

from __future__ import annotations

import re
from typing import Tuple


def _tokenize(text: str) -> set[str]:
    """Simple word tokenizer."""
    if not text:
        return set()
    # Lowercase + extract alphanumeric words
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def compute_lexical_similarity(goal: str, recent_text: str) -> float:
    """
    Compute a simple Jaccard-like similarity between the goal and recent text.

    Returns a value between 0.0 and 1.0.
    """
    goal_tokens = _tokenize(goal)
    recent_tokens = _tokenize(recent_text)

    if not goal_tokens or not recent_tokens:
        return 0.0

    intersection = len(goal_tokens & recent_tokens)
    union = len(goal_tokens | recent_tokens)

    return intersection / union if union > 0 else 0.0


def detect_goal_drift(
    original_goal: str,
    recent_messages: list[str],
    threshold: float = 0.40,
    lookback: int = 5,
) -> Tuple[bool, float]:
    """
    Detect whether the session has drifted from the original goal.

    Args:
        original_goal: The session goal set at the beginning.
        recent_messages: List of recent user messages (most recent last).
        threshold: Similarity below this value triggers drift.
        lookback: Number of recent messages to consider.

    Returns:
        (drift_detected: bool, similarity: float)
    """
    if not original_goal or not recent_messages:
        return False, 1.0

    # Take the last N messages
    recent_text = " ".join(recent_messages[-lookback:])

    similarity = compute_lexical_similarity(original_goal, recent_text)

    drift_detected = similarity < threshold

    return drift_detected, similarity


def should_check_drift(settings) -> bool:
    """Helper to check if drift detection is enabled."""
    return getattr(settings, "goal_drift_detection_enabled", False)