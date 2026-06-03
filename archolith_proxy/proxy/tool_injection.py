"""Session recall as a proxy-intercepted tool.

Injects a synthetic `__archolith_recall` tool into requests when a
session is active. When the model calls this tool, the proxy intercepts
the call, queries the session graph for relevant facts, and returns
the results as a tool response — then re-sends to upstream for the
model to continue with the recalled context.

This gives the model active recall capability: instead of only seeing
passively injected context, it can ask for specific information from
its session memory on demand.

Architecture:
1. Before forwarding to upstream, inject tool definition into body["tools"]
2. After upstream responds, check if the model called __archolith_recall
3. If so: query session graph → build tool result → re-send to upstream
4. Strip internal tool artifacts from the final response

Gated behind SESSION_RECALL_TOOL_ENABLED=true (default false).
Both streaming and non-streaming interception are implemented.

Requires:
- Step 6: embedding-driven retrieval (for meaningful results)
- Step 7a: fingerprint tool normalization (tool injection doesn't drift fingerprint)
- Step 9: query rewriting (for ambiguous recall questions)
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger()

# The synthetic tool definition injected into requests
RECALL_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "__archolith_recall",
        "description": (
            "Search your session memory for facts about a specific topic. "
            "Use when you need context about something discussed earlier that "
            "isn't in your current context. Returns relevant facts, file states, "
            "and decisions from the current session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to search for in session memory",
                }
            },
            "required": ["question"],
        },
    },
}

# Internal tool name constant
RECALL_TOOL_NAME = "__archolith_recall"


def inject_recall_tool(body: dict[str, Any]) -> dict[str, Any]:
    """Inject the __archolith_recall tool into the request body.

    Adds the tool definition to body["tools"] if not already present.
    Also sets tool_choice to "auto" if it was "required" or a specific
    tool (to allow the model to choose whether to recall).

    Args:
        body: The request body dict (modified in place).

    Returns:
        The modified body dict.
    """
    tools = body.get("tools", [])

    # Don't inject if already present (idempotent)
    for tool in tools:
        if (
            isinstance(tool, dict)
            and tool.get("function", {}).get("name") == RECALL_TOOL_NAME
        ):
            return body

    # Add the recall tool
    if tools:
        body["tools"] = tools + [RECALL_TOOL_DEFINITION]
    else:
        body["tools"] = [RECALL_TOOL_DEFINITION]

    # If tool_choice was set to a specific tool or "required", keep it
    # but also allow the recall tool. Set to "auto" if it was restrictive.
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        # Specific tool required — don't override, but the model can still
        # call recall if it wants (most APIs allow any defined tool with "auto")
        pass
    elif tool_choice == "required":
        # "required" means the model MUST call a tool, but can choose any.
        # This is fine — recall is one option.
        pass

    logger.debug("recall_tool_injected", tool_count=len(body.get("tools", [])))
    return body


def strip_recall_tool(body: dict[str, Any]) -> dict[str, Any]:
    """Remove the __archolith_recall tool from the request body.

    Used when we need to clean up the request body (e.g., before
    returning to the harness or for fingerprinting).

    Args:
        body: The request body dict (modified in place).

    Returns:
        The modified body dict.
    """
    tools = body.get("tools", [])
    if not tools:
        return body

    filtered = [
        t for t in tools
        if not (
            isinstance(t, dict)
            and t.get("function", {}).get("name") == RECALL_TOOL_NAME
        )
    ]

    if len(filtered) < len(tools):
        body["tools"] = filtered
        # Remove empty tools array
        if not body["tools"]:
            del body["tools"]

    return body


def find_recall_tool_call(response_data: dict[str, Any]) -> dict | None:
    """Check if a non-streaming response contains a recall tool call.

    Returns the tool call dict if found, None otherwise.
    """
    choices = response_data.get("choices", [])
    if not choices:
        return None

    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls", [])

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function", {})
        if func.get("name") == RECALL_TOOL_NAME:
            return tc

    return None


def strip_recall_from_response(response_data: dict[str, Any]) -> dict[str, Any]:
    """Remove __archolith_recall tool calls from the response.

    The harness shouldn't see internal proxy tools in the model's output.

    Args:
        response_data: The upstream response dict (modified in place).

    Returns:
        The modified response dict.
    """
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls", [])

    if not tool_calls:
        return response_data

    filtered = [
        tc for tc in tool_calls
        if not (
            isinstance(tc, dict)
            and tc.get("function", {}).get("name") == RECALL_TOOL_NAME
        )
    ]

    if len(filtered) < len(tool_calls):
        message["tool_calls"] = filtered
        # If no tool calls remain, remove the field entirely
        if not filtered:
            del message["tool_calls"]

    return response_data


async def handle_recall_tool_call(
    http_client,
    session_id: str,
    question: str,
    turn_number: int = 0,
) -> str:
    """Handle a __archolith_recall tool call by querying the session graph.

    Uses the same retrieval pipeline as passive context assembly:
    - Embeds the question (with optional query rewriting)
    - Queries session facts with cosine similarity scoring
    - Formats top-K results

    Args:
        http_client: HTTP client for embedding API calls.
        session_id: The session to query.
        question: The question from the tool call parameter.
        turn_number: Current turn number for recency scoring.

    Returns:
        Formatted recall results as a string (tool result content).
    """
    from archolith_proxy.assembler.context import (
        _budget_facts,
        _format_relevant_facts,
        _get_query_embedding,
    )
    from archolith_proxy.assembler.query_rewrite import needs_rewrite, rewrite_query, extract_recent_exchanges
    from archolith_proxy.config import get_settings
    from archolith_proxy.graph.backend import get_backend

    settings = get_settings()

    # Apply query rewriting if enabled and the question is ambiguous
    effective_question = question
    if settings.query_rewrite_enabled and needs_rewrite(question):
        # No recent exchanges available in tool context — rewrite with just the question
        # (less context than passive assembly, but still resolves pronouns from session goal)
        try:
            # Use a minimal context: just the question itself
            rewritten = await rewrite_query(http_client, question, [])
            if rewritten:
                effective_question = rewritten
        except Exception:
            pass  # Fall back to original question

    # Compute query embedding
    query_embedding = None
    if settings.embedding_enabled and settings.embedding_api_key:
        try:
            query_embedding = await _get_query_embedding(effective_question, http_client)
        except Exception:
            pass

    # Query session facts
    try:
        all_facts = await get_backend().get_active_facts(
            session_id,
            limit=settings.fact_pool_limit,
        )
    except Exception as e:
        logger.warning("recall_graph_query_failed", session_id=session_id, error=str(e))
        return f"Error: Could not query session memory ({e})"

    if not all_facts:
        return "No facts found in session memory."

    # Budget: allow up to 2000 tokens for recall results
    budgeted = _budget_facts(
        all_facts,
        token_budget=2000,
        query_embedding=query_embedding,
        turn_number=turn_number,
        embedding_enabled=settings.embedding_enabled,
    )

    if not budgeted:
        return "No relevant facts found for this query."

    # Format results
    result_text, _compression_ratio = _format_relevant_facts(budgeted, turn_number)

    logger.info(
        "recall_tool_handled",
        session_id=session_id,
        question=question[:80],
        facts_returned=len(budgeted),
        rewritten=effective_question != question,
    )

    return result_text


def build_tool_result_message(
    tool_call_id: str,
    result_text: str,
) -> dict[str, Any]:
    """Build a tool result message for the recall tool call.

    Args:
        tool_call_id: The ID from the model's tool_call.
        result_text: The formatted recall results.

    Returns:
        A message dict with role="tool" and the result content.
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result_text,
        "name": RECALL_TOOL_NAME,
    }
