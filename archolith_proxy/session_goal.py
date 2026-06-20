"""Session-goal sanitization for prompt-bound context."""

from __future__ import annotations

import re

_DEFAULT_GOAL = "Assist with the current user task."
_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\b(system|developer)\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bexfiltrat(e|ion)\b", re.IGNORECASE),
    re.compile(r"\b(read|print|dump|reveal)\s+(secrets?|api\s+keys?|tokens?|\.env)\b", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(system|developer|admin)\b", re.IGNORECASE),
)
_TAG_RE = re.compile(r"</?(?:system|developer|assistant|user|tool)[^>]*>", re.IGNORECASE)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_ROLE_PREFIX_RE = re.compile(r"\b(?:system|developer|assistant|user|tool)\s*:\s*", re.IGNORECASE)


def sanitize_session_goal(value: str | None, *, max_chars: int = 120) -> str:
    """Return a prompt-safe, single-line session goal.

    Normal task descriptions are preserved. Obvious prompt-injection commands are
    collapsed to a neutral label before they can become persistent curator context.
    """
    if not value:
        return ""

    goal = _FENCE_RE.sub(" ", str(value))
    goal = _TAG_RE.sub(" ", goal)
    goal = _ROLE_PREFIX_RE.sub(" ", goal)
    goal = " ".join(goal.split())
    if not goal:
        return ""

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(goal):
            return _DEFAULT_GOAL

    sentence_end = re.search(r"(?<=[.!?])\s", goal)
    if sentence_end:
        goal = goal[: sentence_end.start() + 1]

    return goal[:max_chars].strip()
