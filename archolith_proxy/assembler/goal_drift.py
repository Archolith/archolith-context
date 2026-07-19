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
    """Normalize words into a compact set of task-relevant lexical signals."""
    if not text:
        return set()

    ignored = {"a", "an", "the", "and", "for", "with", "from", "into", "on", "in", "to", "of", "user", "system", "build", "implement", "continue", "working", "using", "add", "fix", "start", "new"}
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word.startswith("auth"):
            word = "auth"
        elif word.endswith("s") and len(word) > 3:
            word = word[:-1]
        if word not in ignored:
            tokens.add(word)

    # Common authentication terms represent one task domain even when their
    # surface forms differ between the goal and the latest user message.
    if tokens & {"auth", "jwt", "token", "login", "refresh"}:
        tokens.add("__auth_domain__")
    return tokens


def compute_lexical_similarity(goal: str, recent_text: str) -> float:
    """
    Compute task-goal coverage in the recent text.

    Returns a value between 0.0 and 1.0.
    """
    goal_tokens = _tokenize(goal)
    recent_tokens = _tokenize(recent_text)

    if not goal_tokens or not recent_tokens:
        return 0.0

    intersection = len(goal_tokens & recent_tokens)
    return intersection / min(len(goal_tokens), len(recent_tokens))


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
