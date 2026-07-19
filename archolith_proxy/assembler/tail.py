"""Smart coherence tail — prevents orphaned tool messages.

The coherence tail is the last N messages from a conversation, preserved
to keep the model grounded in recent context. A naive slice (rest[-N:])
can start mid-tool-call sequence, creating an orphaned `tool` role message
with no matching `assistant` tool_call — causing 400 errors from upstream APIs.

smart_tail() expands the tail to include any matching assistant messages
for tool results, capping expansion at max_size. If the cap is exceeded,
it falls back to a fixed tail (current behavior) and logs a warning.
"""

from __future__ import annotations

import re
from typing import Literal

import structlog

logger = structlog.get_logger()


def classify_turn_intent(user_message: str) -> Literal["continue", "pivot", "neutral"]:
    """
    Lightweight rule-based classifier for turn intent.

    Returns:
        "continue" - user wants to continue previous work
        "pivot"    - user is starting something new or resetting
        "neutral"  - unclear or no strong signal
    """
    if not user_message:
        return "neutral"

    msg = user_message.lower()

    # Strong pivot signals
    pivot_patterns = [
        r"\b(start fresh|start over|new feature|different approach)\b",
        r"\b(ignore|forget|reset|start again)\b",
        r"\blet'?s (start|begin|do something new)\b",
    ]
    for pattern in pivot_patterns:
        if re.search(pattern, msg):
            return "pivot"

    # Strong continue signals
    continue_patterns = [
        r"\b(continue|keep going|do the same|now do|also|next)\b",
        r"\bfix the (failing|broken|error)\b",
        r"\bwhat we were doing\b",
        r"\b(the other file|that file|same thing)\b",
    ]
    for pattern in continue_patterns:
        if re.search(pattern, msg):
            return "continue"

    return "neutral"


def smart_tail(
    messages: list[dict],
    base_size: int,
    max_size: int = 20,
    intent: Literal["continue", "pivot", "neutral"] | None = None,
    intent_adjustment: int = 0,
    min_size: int = 3,
) -> list[dict]:
    """Select a coherence tail that preserves tool-call structural integrity.

    Starts with the last `base_size` messages from the non-system portion.
    If the tail contains orphaned `tool` messages (no matching `assistant`
    message with the same tool_call_id), expands the tail to include the
    matching assistant message and any messages between it and the tail start.

    Args:
        messages: Non-system messages (rest after stripping system message).
        base_size: Desired tail size (coherence_tail_size from settings).
        max_size: Maximum expanded tail size (max_tail_messages from settings).

    Returns:
        A list of messages forming a structurally valid coherence tail.
    """
    if not messages:
        return []

    # Apply intent-based adjustment
    adjusted_base = base_size
    if intent == "continue":
        adjusted_base = base_size + intent_adjustment
    elif intent == "pivot":
        adjusted_base = max(min_size, base_size - intent_adjustment)

    # Start with the last adjusted_base messages
    tail_start = max(0, len(messages) - adjusted_base)
    tail = messages[tail_start:]

    # Find orphaned tool messages and expand to include their matching assistant
    expanded_start = tail_start
    needs_expansion = True
    iterations = 0
    max_iterations = base_size  # Safety limit to prevent infinite loops

    while needs_expansion and iterations < max_iterations:
        needs_expansion = False
        iterations += 1

        # Scan the current tail for tool messages
        for i, msg in enumerate(messages[expanded_start:], start=expanded_start):
            if msg.get("role") != "tool":
                continue

            # This is a tool message — find its matching assistant message
            tool_call_id = msg.get("tool_call_id")
            if not tool_call_id:
                # Tool message without a tool_call_id — strip it defensively
                # (shouldn't happen in valid conversations, but handle gracefully)
                continue

            # Walk backward through the full message array to find the assistant
            # message that issued this tool_call_id
            match_index = _find_assistant_with_tool_call(messages, tool_call_id, expanded_start)

            if match_index is not None and match_index < expanded_start:
                # The matching assistant message is outside the current tail — expand
                expanded_start = match_index
                needs_expansion = True
                break  # Restart scan with new expanded_start

    # Check if expanded tail exceeds max_size
    expanded_size = len(messages) - expanded_start
    if expanded_size > max_size:
        logger.warning(
            "smart_tail_expansion_exceeded_max",
            expanded_size=expanded_size,
            max_size=max_size,
            base_size=base_size,
            fallback="turn_boundary",
        )
        # Fall back to turn-boundary strategy: take the last max_size messages,
        # then advance to the first user message or past leading orphaned tools.
        window_start = max(0, len(messages) - max_size)
        new_start = window_start

        # First, search for the first user message at index >= window_start
        user_found = False
        for i in range(window_start, len(messages)):
            if messages[i].get("role") == "user":
                new_start = i
                user_found = True
                break

        # If no user message found, skip leading orphaned tool messages
        if not user_found:
            for i in range(window_start, len(messages)):
                msg = messages[i]
                if msg.get("role") != "tool":
                    # Not a tool message — stop skipping
                    new_start = i
                    break

                # This is a tool message — check if it's orphaned
                tool_call_id = msg.get("tool_call_id")
                if not tool_call_id:
                    # Tool message without tool_call_id is always orphaned
                    continue

                # Check if there's a matching assistant in the window
                # _find_assistant_with_tool_call searches backward from search_before
                # We want to find a match within [window_start, i), so search_before=i
                match_index = _find_assistant_with_tool_call(messages, tool_call_id, i)
                if match_index is not None and match_index >= window_start:
                    # Matching assistant is in the window — not orphaned
                    new_start = i
                    break
                # else: orphaned, continue skipping

            else:
                # All messages in window were orphaned tools — return from end of window
                # (this edge case results in empty or near-empty tail, but keeps integrity)
                new_start = len(messages)

        return messages[new_start:]

    return messages[expanded_start:]


def _find_assistant_with_tool_call(
    messages: list[dict],
    tool_call_id: str,
    search_before: int,
) -> int | None:
    """Find the index of the assistant message containing a tool_call_id.

    Searches backward from search_before in the messages array.

    Args:
        messages: Full message array.
        tool_call_id: The tool_call_id to match.
        search_before: Only search indices before this position.

    Returns:
        Index of the matching assistant message, or None if not found.
    """
    for i in range(search_before - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "assistant":
            continue

        # Check if this assistant message has tool_calls with the matching ID
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            continue

        for tc in tool_calls:
            if isinstance(tc, dict) and tc.get("id") == tool_call_id:
                return i

    return None
