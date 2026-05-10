"""Session fingerprinting + session resolution.

Primary: X-Session-ID header (explicit, stable).
Fallback: SHA-256(sanitized_system_prompt + first_user_message)[:16].

System prompt sanitization strips timestamps, dates, and other dynamic
content to prevent fingerprint drift between turns.
"""

from __future__ import annotations

import hashlib
import re

import structlog

from src.graph import session as session_repo

logger = structlog.get_logger()

# Patterns to strip from system prompts before fingerprinting
_SANITIZE_PATTERNS = [
    re.compile(r"(?m)^.*current\s+(date|time|timestamp)\s*[:=].*$", re.IGNORECASE),
    re.compile(r"(?m)^.*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}.*$", re.IGNORECASE),
    re.compile(r"(?m)^.*today'?s?\s+date\s*[:=].*$", re.IGNORECASE),
]


def sanitize_system_prompt(prompt: str) -> str:
    """Strip dynamic content from system prompt for stable fingerprinting."""
    cleaned = prompt
    for pattern in _SANITIZE_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    # Collapse multiple blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def compute_fingerprint(system_prompt: str, first_user_message: str) -> str:
    """Compute session fingerprint from sanitized system prompt + first user message."""
    sanitized = sanitize_system_prompt(system_prompt)
    raw = f"{sanitized}\n{first_user_message}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def resolve_session(
    headers: dict,
    messages: list[dict],
) -> tuple[str, bool]:
    """Resolve or create a session for this request.

    Returns (session_id, is_new_session).
    """
    # Primary: explicit X-Session-ID header
    session_id = headers.get("x-session-id") or headers.get("X-Session-ID")
    if session_id:
        existing = await session_repo.find_by_session_id(session_id)
        if existing:
            await session_repo.touch_session(session_id)
            return session_id, False
        # Create with explicit ID
        await session_repo.create_session(session_id, fingerprint=None)
        return session_id, True

    # Fallback: fingerprint from messages
    system_msg = ""
    first_user_msg = ""
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if role == "system" and not system_msg:
            system_msg = content or ""
        elif role == "user" and not first_user_msg:
            first_user_msg = content or ""
            break

    if not first_user_msg:
        # No user message — can't fingerprint, generate random
        import uuid
        session_id = uuid.uuid4().hex[:16]
        await session_repo.create_session(session_id, fingerprint=None)
        return session_id, True

    fingerprint = compute_fingerprint(system_msg, first_user_msg)

    # Look up existing session
    existing = await session_repo.find_by_fingerprint(fingerprint)
    if existing:
        session_id = existing["session_id"]
        await session_repo.touch_session(session_id)
        return session_id, False

    # Create new session
    import uuid
    session_id = uuid.uuid4().hex[:16]
    await session_repo.create_session(session_id, fingerprint=fingerprint)
    logger.info("session_created", session_id=session_id, fingerprint=fingerprint)
    return session_id, True
