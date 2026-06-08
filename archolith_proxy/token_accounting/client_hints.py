"""Client hint intake — accept and validate optional token hints from callers.

Clients (OpenCode, Claude Code, Aider, etc.) may know their own session
token counts. This module parses, validates, and normalizes those hints
so the proxy can display both numbers without conflating them.

Client hints are always stored SEPARATELY from proxy estimates.
They never overwrite proxy-side counts. They MAY influence the gate
decision (as a secondary input via max(structural, client_reported)).

Supported hint mechanisms:
1. HTTP header: X-Context-Token-Hint: <int>
2. Request body metadata: _meta.context_token_hint: <int>
3. Both are optional and ignored if absent or invalid.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

# Header name for client token hints
TOKEN_HINT_HEADER = "x-context-token-hint"

# Metadata key for request-body hints
TOKEN_HINT_META_KEY = "context_token_hint"

# Reasonable bounds for validation — reject obviously wrong hints
MIN_REASONABLE_TOKENS = 100    # Below this, the hint is likely a mistake
MAX_REASONABLE_TOKENS = 5000000  # 5M tokens — well above any real session


def parse_client_hint_header(headers: dict) -> int | None:
    """Parse token hint from an HTTP header.

    Args:
        headers: The request headers (case-insensitive lookup).

    Returns:
        Validated token count, or None if absent/invalid.
    """
    # Headers may be case-insensitive depending on the framework
    value = None
    for key, val in headers.items():
        if key.lower() == TOKEN_HINT_HEADER:
            value = val
            break

    if value is None:
        return None

    try:
        tokens = int(value)
    except (ValueError, TypeError):
        logger.warning(
            "client_hint_header_invalid",
            header=TOKEN_HINT_HEADER,
            value=str(value)[:50],
        )
        return None

    return _validate_hint(tokens, source="header")


def parse_client_hint_meta(body: dict) -> int | None:
    """Parse token hint from request body metadata.

    Looks for body._meta.context_token_hint or body.context_token_hint.

    Args:
        body: The parsed request body.

    Returns:
        Validated token count, or None if absent/invalid.
    """
    # Try _meta namespace first
    meta = body.get("_meta", {})
    if isinstance(meta, dict):
        value = meta.get(TOKEN_HINT_META_KEY)
        if value is not None:
            return _parse_and_validate(value, source="meta")

    # Try top-level
    value = body.get(TOKEN_HINT_META_KEY)
    if value is not None:
        return _parse_and_validate(value, source="body_top_level")

    return None


def extract_client_hint(headers: dict, body: dict) -> int | None:
    """Extract client token hint from any available source.

    Priority: header > meta > top-level body.
    Returns the first valid hint found, or None.

    Args:
        headers: Request headers.
        body: Parsed request body.

    Returns:
        Validated token count, or None if no valid hint found.
    """
    # Try header first
    hint = parse_client_hint_header(headers)
    if hint is not None:
        return hint

    # Try metadata
    hint = parse_client_hint_meta(body)
    if hint is not None:
        return hint

    return None


def _parse_and_validate(value, source: str = "unknown") -> int | None:
    """Parse and validate a token hint value."""
    try:
        tokens = int(value)
    except (ValueError, TypeError):
        logger.warning(
            "client_hint_invalid",
            source=source,
            value=str(value)[:50],
        )
        return None

    return _validate_hint(tokens, source=source)


def _validate_hint(tokens: int, source: str = "unknown") -> int | None:
    """Validate a token hint is within reasonable bounds.

    Returns None and logs a warning if the hint is obviously wrong.
    """
    if tokens < MIN_REASONABLE_TOKENS:
        logger.warning(
            "client_hint_too_low",
            source=source,
            value=tokens,
            min=MIN_REASONABLE_TOKENS,
        )
        return None

    if tokens > MAX_REASONABLE_TOKENS:
        logger.warning(
            "client_hint_too_high",
            source=source,
            value=tokens,
            max=MAX_REASONABLE_TOKENS,
        )
        return None

    logger.debug("client_hint_accepted", source=source, tokens=tokens)
    return tokens
