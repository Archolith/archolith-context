"""Unified recall interception — handles __archolith_recall tool call interception.

Extracted from openai/chat.py where the logic was duplicated between the
streaming path and the non-streaming path. Provides a single interface
that both paths call.

A recall interception workflow:
1. Detect __archolith_recall tool call in the response
2. Parse the question from the tool call arguments
3. Execute the recall query via handle_recall_tool_call
4. Build a re-send message array (original messages + model response + tool result)
5. Re-send to upstream as non-streaming
6. Handle a potential second recall (limited to 2 per turn)
7. Return the final response with metadata for observability
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from archolith_proxy.metrics import get_metrics, record_metric
from archolith_proxy.filter_adapter import filter_request_body

logger = structlog.get_logger()

__all__ = [
    "RecallResult",
    "execute_recall",
    "build_resend_messages",
    "resend_with_recall",
    "detect_recall_trigger",
    "inject_proxy_recall_into_body",
    "handle_non_streaming_recall",
]


@dataclass
class RecallResult:
    """Result of a non-streaming recall interception.

    Carries the final upstream response data (after all recall rounds)
    alongside metadata needed for observability: which questions were
    asked and how many facts each round returned.
    """

    final_data: dict[str, Any] | None  # Final response dict, or None if no recall
    recall_used: bool = False
    recall_questions: list[str] = field(default_factory=list)
    facts_returned_counts: list[int] = field(default_factory=list)


async def execute_recall(
    http_client: httpx.AsyncClient,
    tool_call: dict[str, Any],
    session_id: str,
    turn_number: int,
) -> str | None:
    """Execute a recall query from a tool call.

    Returns the recall result text, or None on failure/empty question.
    """
    from archolith_proxy.proxy.tool_injection import handle_recall_tool_call

    func = tool_call.get("function", {})
    try:
        args = json.loads(func.get("arguments", "{}"))
        question = args.get("question", "")
    except (json.JSONDecodeError, TypeError):
        question = ""

    if not question:
        logger.warning("recall_empty_question", session_id=session_id)
        return None

    recall_text = await handle_recall_tool_call(
        http_client=http_client,
        session_id=session_id,
        question=question,
        turn_number=turn_number,
    )
    return recall_text


def build_resend_messages(
    original_messages: list[dict],
    model_message: dict[str, Any],
    tool_call: dict[str, Any],
    recall_text: str,
) -> list[dict]:
    """Build the re-send message array for a recall interception.

    Takes the original messages, the model's response (with the recall tool call),
    and the tool result, and produces the message array for the re-send request.

    The assistant message MUST retain the recall tool_call in its tool_calls array
    because the OpenAI API requires every role="tool" message to have a matching
    tool_call_id in the preceding assistant message. Stripping the tool_call from
    the assistant message while keeping the tool result causes a 400 Bad Request.

    The recall tool is already removed from the tools *definition* array by
    strip_recall_tool() in resend_with_recall(), so the model cannot call it
    again in the re-send — it just sees the completed tool call in history.
    """
    from archolith_proxy.proxy.tool_injection import (
        build_tool_result_message,
    )

    resend_messages = list(original_messages)

    # Keep the assistant message as-is (including the recall tool_call) so the
    # tool result message has a matching tool_call_id in the conversation history.
    resend_model_msg = dict(model_message)
    resend_messages.append(resend_model_msg)
    resend_messages.append(
        build_tool_result_message(tool_call.get("id", "recall_0"), recall_text)
    )
    return resend_messages


async def resend_with_recall(
    http_client: httpx.AsyncClient,
    url: str,
    headers: dict,
    original_body: bytes,
    resend_messages: list[dict],
    session_id: str,
    turn_number: int,
    max_retries: int = 3,
    backoff_base: float = 0.5,
    recall_questions: list[str] | None = None,
    facts_returned_counts: list[int] | None = None,
) -> tuple[dict[str, Any] | None, list[str], list[int]]:
    """Re-send a request with the recall tool result appended.

    Handles one level of second-recall (max 2 total per turn to prevent loops).

    Returns:
        Tuple of (final response data or None, recall questions asked, facts returned per question).
    """
    from archolith_proxy.proxy.tool_injection import (
        RECALL_TOOL_NAME,
        find_recall_tool_call,
        handle_recall_tool_call,
        strip_recall_from_response,
        strip_recall_tool,
        build_tool_result_message,
    )
    from archolith_proxy.proxy.upstream import upstream_request_with_retry
    from archolith_proxy.config import get_settings

    # Track recall metadata across rounds
    tracked_questions: list[str] = list(recall_questions or [])
    tracked_facts: list[int] = list(facts_returned_counts or [])
    settings = get_settings()

    # Strip the recall tool from the tools array for the re-send
    body_dict = json.loads(original_body)
    strip_recall_tool(body_dict)

    current_messages = resend_messages

    for depth in range(2):  # Max 2 rounds of recall
        resend_payload = {
            **body_dict,
            "stream": False,
            "messages": current_messages,
        }
        resend_payload = filter_request_body(resend_payload, enabled=settings.filter_enabled)
        # Debug: log the message structure being sent for the resend
        msg_summary = []
        for m in current_messages:
            role = m.get("role", "?")
            has_tc = "tool_calls" in m and m["tool_calls"]
            tc_ids = [tc.get("id", "?") for tc in (m.get("tool_calls") or [])] if has_tc else []
            tc_id = m.get("tool_call_id", "")
            msg_summary.append(f"{role}(tc={tc_ids},tcid={tc_id})" if (has_tc or tc_id) else role)
        logger.debug(
            "recall_resend_messages",
            depth=depth,
            message_roles=msg_summary,
            has_tools="tools" in resend_payload,
        )
        resend_body = json.dumps(resend_payload).encode("utf-8")

        try:
            resp = await upstream_request_with_retry(
                client=http_client,
                url=url,
                headers=headers,
                content=resend_body,
                max_retries=max_retries,
                backoff_base=backoff_base,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            record_metric("upstream_errors", 1)
            logger.warning(
                "recall_resend_failed",
                session_id=session_id, turn=turn_number, depth=depth, error=str(e),
            )
            return None, tracked_questions, tracked_facts

        if resp.status_code >= 400:
            record_metric("upstream_errors", 1)
            error_body = ""
            try:
                error_body = resp.text[:500]
            except Exception:
                pass
            logger.warning(
                "recall_resend_error",
                session_id=session_id, turn=turn_number, depth=depth,
                status=resp.status_code, error_body=error_body,
            )
            return None, tracked_questions, tracked_facts

        data = resp.json()

        # Check for second recall
        second_tc = find_recall_tool_call(data)
        if second_tc is None:
            # No more recall calls — done
            strip_recall_from_response(data)
            return data, tracked_questions, tracked_facts

        # Log second recall
        logger.info(
            "recall_second_call",
            session_id=session_id, turn=turn_number, depth=depth + 1,
        )
        get_metrics()["recall_tool_calls"] = get_metrics().get("recall_tool_calls", 0) + 1

        # Extract second recall question
        func = second_tc.get("function", {})
        try:
            args = json.loads(func.get("arguments", "{}"))
            second_question = args.get("question", "")
        except (json.JSONDecodeError, TypeError):
            second_question = ""

        if not second_question:
            strip_recall_from_response(data)
            return data, tracked_questions, tracked_facts

        tracked_questions.append(second_question)

        # Execute second recall
        second_recall_text = await handle_recall_tool_call(
            http_client=http_client,
            session_id=session_id,
            question=second_question,
            turn_number=turn_number,
        )

        # Count approximate facts from the recall text
        # (Placeholder — the actual count requires parsing the graph query result,
        # which handle_recall_tool_call doesn't currently expose.)
        tracked_facts.append(0)

        # Build next message array for third request — keep the assistant's
        # tool_calls intact so the tool result has a matching tool_call_id.
        second_model_msg = data["choices"][0]["message"]
        third_msg = dict(second_model_msg)

        current_messages = list(current_messages) + [third_msg, build_tool_result_message(
            second_tc.get("id", "recall_1"), second_recall_text,
        )]

    # Exhausted depth limit — return whatever we got
    strip_recall_from_response(data)
    return data, tracked_questions, tracked_facts


# ---------------------------------------------------------------------------
# Proxy-forced recall — trigger detection and body injection
# ---------------------------------------------------------------------------

# Phrases in the last user message that signal the model wants remembered context.
# Matched case-insensitively against the lowercased user message.
_RECALL_PHRASES: tuple[str, ...] = (
    "what did we decide",
    "what was decided",
    "what did we agree",
    "what was our plan",
    "remind me",
    "what have we done",
    "earlier in this session",
    "from earlier",
    "from memory",
    "what did you say about",
    "what was the decision",
    "what was agreed",
)


def detect_recall_trigger(
    messages: list[dict[str, Any]],
    is_user_turn: bool,
) -> tuple[str, str] | None:
    """Return (trigger_type, query) if a proxy-recall should fire, else None.

    Trigger types:
    - "user_phrase"        — last user message contains explicit recall language.
    - "repeated_file_read" — the same file path appears in ≥2 tool results in
                             the last 20 messages, suggesting the model is reading
                             the same file again because context was lost.

    Only fires on user turns; agent-solo turns are never auto-recalled because
    they are continuation turns where the model already has recent context.
    """
    if not is_user_turn or not messages:
        return None

    # ── Trigger 1: recall phrase in last user message ──────────────────────
    last_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                last_user_text = content.lower()
            elif isinstance(content, list):
                last_user_text = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                ).lower()
            break

    if last_user_text:
        for phrase in _RECALL_PHRASES:
            if phrase in last_user_text:
                # Use the raw user text (truncated) as the recall query so the
                # graph retrieval is anchored to what the user is asking about.
                return ("user_phrase", last_user_text[:200])

    # ── Trigger 2: repeated file reads in recent tool messages ─────────────
    # Extract filenames from tool messages named "read_file" or "Read".
    # The first line of many read_file results is the file path.
    file_hit_counts: dict[str, int] = {}
    for m in messages[-20:]:
        if m.get("role") != "tool":
            continue
        tool_name = m.get("name", "").lower()
        if "read" not in tool_name:
            continue
        content = m.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        # Heuristic: first non-blank line often contains path or filename header.
        for line in content.splitlines():
            line = line.strip()
            if line and len(line) < 300:
                file_hit_counts[line] = file_hit_counts.get(line, 0) + 1
                break  # Only use first identifying line per message

    repeated = sorted(
        (f for f, c in file_hit_counts.items() if c >= 2),
        key=lambda f: -file_hit_counts[f],
    )
    if repeated:
        file_list = "; ".join(repeated[:3])
        return ("repeated_file_read", f"file content and recent context for: {file_list}")

    return None


def inject_proxy_recall_into_body(
    body: dict[str, Any],
    recall_text: str,
    trigger: str,
) -> dict[str, Any]:
    """Append a compact proxy-recall block to the system message.

    If no system message exists, one is created.  The block is clearly
    bracketed so the model can distinguish injected session memory from
    explicit user instructions.
    """
    recall_block = (
        f"\n\n[PROXY-RECALL | trigger={trigger}]\n"
        f"{recall_text}\n"
        "[END PROXY-RECALL]"
    )

    messages = list(body.get("messages", []))
    if not messages:
        return body

    new_messages: list[dict[str, Any]] = []
    injected = False
    for m in messages:
        if not injected and m.get("role") == "system":
            m = dict(m)
            content = m.get("content", "")
            if isinstance(content, str):
                m["content"] = content + recall_block
            else:
                # Multi-part system message — append as a text part
                m["content"] = list(content) + [{"type": "text", "text": recall_block.strip()}]
            injected = True
        new_messages.append(m)

    if not injected:
        # No system message exists — prepend a minimal one
        new_messages = [{"role": "system", "content": recall_block.strip()}] + new_messages

    body = dict(body)
    body["messages"] = new_messages
    return body


async def handle_non_streaming_recall(
    resp: httpx.Response,
    http_client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: bytes,
    session_id: str,
    turn_number: int,
    original_messages: list[dict],
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> RecallResult:
    """Handle recall interception for a non-streaming response.

    If the response contains a __archolith_recall call, intercept,
    execute the recall, and re-send (handling up to 2 recall rounds).

    Returns:
        RecallResult with final_data (None if no recall was needed),
        recall_used flag, questions asked, and facts returned per question.
    """
    from archolith_proxy.proxy.tool_injection import find_recall_tool_call

    data = resp.json()
    tool_call = find_recall_tool_call(data)
    if tool_call is None:
        return RecallResult(final_data=None, recall_used=False)

    logger.info(
        "non_streaming_recall_intercepted",
        session_id=session_id, turn=turn_number,
    )
    get_metrics()["recall_tool_calls"] = get_metrics().get("recall_tool_calls", 0) + 1

    # Extract the first recall question for observability tracking
    func = tool_call.get("function", {})
    try:
        args = json.loads(func.get("arguments", "{}"))
        first_question = args.get("question", "")
    except (json.JSONDecodeError, TypeError):
        first_question = ""

    recall_questions: list[str] = [first_question] if first_question else []
    facts_returned_counts: list[int] = [0] if first_question else []

    recall_text = await execute_recall(
        http_client=http_client,
        tool_call=tool_call,
        session_id=session_id,
        turn_number=turn_number,
    )
    if recall_text is None:
        return RecallResult(final_data=None, recall_used=True, recall_questions=recall_questions)

    model_message = data["choices"][0]["message"]
    resend_messages = build_resend_messages(
        original_messages=original_messages,
        model_message=model_message,
        tool_call=tool_call,
        recall_text=recall_text,
    )

    final_data, tracked_questions, tracked_facts = await resend_with_recall(
        http_client=http_client,
        url=url,
        headers=headers,
        original_body=body,
        resend_messages=resend_messages,
        session_id=session_id,
        turn_number=turn_number,
        max_retries=max_retries,
        backoff_base=backoff_base,
        recall_questions=recall_questions,
        facts_returned_counts=facts_returned_counts,
    )

    return RecallResult(
        final_data=final_data,
        recall_used=True,
        recall_questions=tracked_questions,
        facts_returned_counts=tracked_facts,
    )
