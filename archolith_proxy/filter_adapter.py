"""Thin outbound filter adapter for proxy-side tool-result filtering and shrinking."""

from __future__ import annotations

import enum
from typing import Any, Callable

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Sentinel type — explicit 3-state for unresolved/callable/unavailable
# ---------------------------------------------------------------------------

class _LoadState(enum.Enum):
    UNRESOLVED = enum.auto()

_UNRESOLVED = _LoadState.UNRESOLVED

# ---------------------------------------------------------------------------
# Lazy loaders — each sentinel starts as UNRESOLVED, becomes a
# callable on first successful import, or None if the package is absent.
# ---------------------------------------------------------------------------

_filter_output_fn: Callable[..., Any] | None | _LoadState = _UNRESOLVED
_shrink_args_fn: Callable[..., Any] | None | _LoadState = _UNRESOLVED
_shrink_results_fn: Callable[..., Any] | None | _LoadState = _UNRESOLVED


def _load_filter_output() -> Callable[..., Any] | None:
    """Lazy-load archolith_filter.filter_output and fail open if unavailable."""
    global _filter_output_fn
    if _filter_output_fn is _UNRESOLVED:
        try:
            from archolith_filter import filter_output as loaded

            _filter_output_fn = loaded
        except ImportError:
            logger.warning(
                "filter_dependency_missing",
                message="archolith_filter is not installed; filter disabled",
            )
            _filter_output_fn = None
    return _filter_output_fn if callable(_filter_output_fn) else None


def _load_shrink_functions() -> tuple[Callable[..., Any] | None, Callable[..., Any] | None]:
    """Lazy-load archolith_filter shrink functions and fail open if unavailable.

    Returns (shrink_args_fn, shrink_results_fn).
    """
    global _shrink_args_fn, _shrink_results_fn
    if _shrink_args_fn is _UNRESOLVED:
        try:
            from archolith_filter.shrink import (
                shrink_oversized_tool_call_args_by_tokens,
                shrink_oversized_tool_results_by_tokens,
            )

            _shrink_args_fn = shrink_oversized_tool_call_args_by_tokens
            _shrink_results_fn = shrink_oversized_tool_results_by_tokens
        except ImportError:
            _shrink_args_fn = None
            _shrink_results_fn = None
    return (
        _shrink_args_fn if callable(_shrink_args_fn) else None,
        _shrink_results_fn if callable(_shrink_results_fn) else None,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_available() -> bool:
    """Return True if archolith_filter is installed and the filter function is callable.

    Used by the trace builder to distinguish 'filter enabled but package missing
    (fail-open)' from 'filter enabled and active'.
    """
    return _load_filter_output() is not None


def filter_tool_messages(messages: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    """Apply Layer 1 filtering to outbound tool-role messages."""
    if not enabled:
        return messages

    filter_output = _load_filter_output()
    if filter_output is None:
        return messages

    filtered_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "tool":
            filtered_messages.append(message)
            continue

        content = message.get("content")
        if not isinstance(content, str):
            filtered_messages.append(message)
            continue

        tool_name = str(message.get("name") or "tool_result")
        try:
            result = filter_output(content, tool=tool_name)
        except Exception as exc:
            logger.warning("filter_failed", tool=tool_name, error=str(exc))
            filtered_messages.append(message)
            continue

        filtered_message = dict(message)
        filtered_message["content"] = result.output
        filtered_messages.append(filtered_message)

    return filtered_messages


def filter_single_tool_result(content: str, tool_name: str = "unknown") -> str:
    """Apply Layer 1 filter to a single tool result string.

    Used by the extraction pipeline to strip noise before the extractor LLM
    processes tool results.  Fail-open: returns content unchanged if filter is
    unavailable or the filter raises.
    """
    filter_output = _load_filter_output()
    if filter_output is None:
        return content
    try:
        result = filter_output(content, tool=tool_name)
        return result.output
    except Exception as exc:
        logger.debug("filter_single_failed", tool=tool_name, error=str(exc))
        return content


def _unwrap_shrink_result(result: Any) -> list[dict[str, Any]]:
    """Normalise filter shrink return values to list[dict].

    filter shrink functions return ShrinkTokensResult / ShrinkCharsResult dataclasses
    with a ``.messages`` field.  Earlier versions returned list[ChatMessage] directly.
    This helper handles both cases and converts ChatMessage → dict when needed.
    """
    # ShrinkTokensResult / ShrinkCharsResult — extract .messages
    if hasattr(result, "messages"):
        msgs = result.messages
    elif isinstance(result, list):
        msgs = result
    else:
        return result  # type: ignore[return-value]

    return [m.to_dict() if hasattr(m, "to_dict") else m for m in msgs]


def shrink_tool_call_args(
    messages: list[dict[str, Any]],
    max_tokens: int = 500,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Shrink oversized tool_call arguments in assistant messages.

    Collapses large Write/Edit args (file content, patch bodies) that bloat
    history once the file is already in the content cache.
    Fail-open: returns messages unchanged if filter unavailable or shrink raises.
    """
    if not enabled:
        return messages
    shrink_args, _ = _load_shrink_functions()
    if shrink_args is None:
        return messages
    try:
        result = shrink_args(messages, max_tokens=max_tokens)
        return _unwrap_shrink_result(result)
    except Exception as exc:
        logger.debug("filter_shrink_args_failed", error=str(exc))
        return messages


def shrink_tail_tool_results(
    messages: list[dict[str, Any]],
    max_tokens_per_result: int = 2000,
) -> list[dict[str, Any]]:
    """Token-budget each tool-role message in the coherence tail.

    Prevents large file reads and command outputs from dominating the context
    window when retained in the tail for structural integrity.
    Fail-open: returns messages unchanged if filter unavailable or shrink raises.
    """
    _, shrink_results = _load_shrink_functions()
    if shrink_results is None:
        return messages
    try:
        result = shrink_results(messages, max_tokens=max_tokens_per_result)
        return _unwrap_shrink_result(result)
    except Exception as exc:
        logger.debug("filter_shrink_tail_failed", error=str(exc))
        return messages


def filter_request_body(body: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Return a request body with outbound tool messages filtered and args shrunk.

    Applies two filter passes when enabled:
    1. Layer 1 filter on tool-role messages (noise/boilerplate removal)
    2. Shrink oversized tool_call arguments in assistant messages
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body

    processed = filter_tool_messages(messages, enabled=enabled)
    processed = shrink_tool_call_args(processed, enabled=enabled)

    if processed is messages:
        return body

    filtered_body = dict(body)
    filtered_body["messages"] = processed
    return filtered_body
