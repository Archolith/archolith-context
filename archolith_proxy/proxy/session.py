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

from archolith_proxy.graph.backend import get_backend

logger = structlog.get_logger()

# ── Benchmark session-ID override ─────────────────────────────────────────────
# When set, any request that arrives WITHOUT an explicit X-Session-ID header
# is forced to use this session ID instead of the fingerprint fallback.
# Intended for benchmark runs where the caller pre-generates the session ID
# and needs to fetch the exact trace afterwards.
# Only one override can be active at a time (benchmarks are sequential).
_benchmark_session_id: str | None = None
_benchmark_passthrough_session_id: str | None = None


def set_benchmark_session_id(session_id: str) -> None:
    """Activate a benchmark session-ID override."""
    global _benchmark_session_id
    _benchmark_session_id = session_id
    logger.info("benchmark_session_override_set", session_id=session_id)


def clear_benchmark_session_id() -> None:
    """Clear the benchmark session-ID override."""
    global _benchmark_session_id
    _benchmark_session_id = None
    logger.info("benchmark_session_override_cleared")


def get_benchmark_session_id() -> str | None:
    """Return the current benchmark override, or None."""
    return _benchmark_session_id


def set_benchmark_passthrough_session_id(session_id: str) -> None:
    """Activate a benchmark passthrough session-ID override."""
    global _benchmark_passthrough_session_id
    _benchmark_passthrough_session_id = session_id
    logger.info("benchmark_passthrough_session_override_set", session_id=session_id)


def clear_benchmark_passthrough_session_id() -> None:
    """Clear the benchmark passthrough session-ID override."""
    global _benchmark_passthrough_session_id
    _benchmark_passthrough_session_id = None
    logger.info("benchmark_passthrough_session_override_cleared")


def get_benchmark_passthrough_session_id() -> str | None:
    """Return the current passthrough benchmark override, or None."""
    return _benchmark_passthrough_session_id


# ── Patterns to strip from system prompts before fingerprinting ───────────────
_SANITIZE_PATTERNS = [
    re.compile(r"(?m)^.*current\s+(date|time|timestamp)\s*[:=].*$", re.IGNORECASE),
    re.compile(r"(?m)^.*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}.*$", re.IGNORECASE),
    re.compile(r"(?m)^.*today'?s?\s+date\s*[:=].*$", re.IGNORECASE),
    # Tool definition blocks embedded in system prompts (harnesses that inject
    # tool descriptions cause fingerprint drift between turns)
    re.compile(
        r"(?ms)^(?:#\s*)?(?:available\s+tools?\s*[:=]|tool\s+definitions?\s*[:=]|tools?\s*[:=])\s*\[.*?\]",
        re.IGNORECASE,
    ),
    # JSON tool schema lines (e.g., "\"name\": \"read_file\", ...")
    re.compile(r'(?m)^\s*"(?:name|description|parameters)"\s*:\s*".*?(?:",|"\s*[,}])\s*$'),
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
        existing = await get_backend().find_session_by_id(session_id)
        if existing:
            await get_backend().touch_session(session_id)
            return session_id, False
        # Create with explicit ID
        await get_backend().create_session(session_id, fingerprint=None)
        return session_id, True

    # Secondary: benchmark override (set via /trace/benchmark/session-id admin endpoint)
    # Allows benchmark scripts to pre-register a known session ID so the trace
    # can be fetched by that ID after the run, without needing to control headers.
    benchmark_id = get_benchmark_session_id()
    if benchmark_id:
        existing = await get_backend().find_session_by_id(benchmark_id)
        if existing:
            await get_backend().touch_session(benchmark_id)
            return benchmark_id, False
        await get_backend().create_session(benchmark_id, fingerprint=None)
        logger.info("benchmark_session_created", session_id=benchmark_id)
        return benchmark_id, True

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
        await get_backend().create_session(session_id, fingerprint=None)
        return session_id, True

    fingerprint = compute_fingerprint(system_msg, first_user_msg)

    # Atomic find-or-create: MERGE avoids the lookup-then-create race when
    # two concurrent first-turn requests arrive with the same fingerprint.
    session_data, is_new = await get_backend().find_or_create_by_fingerprint(fingerprint)
    session_id = session_data.get("session_id", "")

    if is_new:
        logger.info("session_created", session_id=session_id, fingerprint=fingerprint)
    else:
        await get_backend().touch_session(session_id)

    return session_id, is_new
