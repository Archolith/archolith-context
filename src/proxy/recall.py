"""Unified recall interception — handles __context_engine_recall tool call interception.

Extracted from openai/chat.py where the logic was duplicated between the
streaming path and the non-streaming path. Provides a single interface
that both paths call.

A recall interception workflow:
1. Detect __context_engine_recall tool call in the response
2. Parse the question from the tool call arguments
3. Execute the recall query via handle_recall_tool_call
4. Build a re-send message array (original messages + model response + tool result)
5. Re-send to upstream as non-streaming
6. Handle a potential second recall (limited to 2 per turn)
7. Return the final response
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from src.metrics import get_metrics, record_metric

logger = structlog.get_logger()


async def execute_recall(
    http_client: httpx.AsyncClient,
    tool_call: dict[str, Any],
    session_id: str,
    turn_number: int,
) -> str | None:
    """Execute a recall query from a tool call.

    Returns the recall result text, or None on failure/empty question.
    """
    from src.proxy.tool_injection import handle_recall_tool_call

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
    """
    from src.proxy.tool_injection import (
        RECALL_TOOL_NAME,
        build_tool_result_message,
    )

    # Strip the recall tool call from the model message
    remaining_calls = [
        tc for tc in model_message.get("tool_calls", [])
        if tc.get("function", {}).get("name") != RECALL_TOOL_NAME
    ]

    resend_messages = list(original_messages)
    resend_model_msg = dict(model_message)
    if remaining_calls:
        resend_model_msg["tool_calls"] = remaining_calls
    else:
        resend_model_msg.pop("tool_calls", None)
    if not remaining_calls and not resend_model_msg.get("content"):
        resend_model_msg["content"] = None
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
) -> dict[str, Any] | None:
    """Re-send a request with the recall tool result appended.

    Handles one level of second-recall (max 2 total per turn to prevent loops).
    Returns the final response data, or None on failure.
    """
    from src.proxy.tool_injection import (
        RECALL_TOOL_NAME,
        find_recall_tool_call,
        strip_recall_from_response,
        strip_recall_tool,
        build_tool_result_message,
    )
    from src.proxy.upstream import upstream_request_with_retry

    # Strip the recall tool from the tools array for the re-send
    body_dict = json.loads(original_body)
    strip_recall_tool(body_dict)

    current_messages = resend_messages

    for depth in range(2):  # Max 2 rounds of recall
        resend_body = json.dumps({
            **body_dict,
            "stream": False,
            "messages": current_messages,
        }).encode("utf-8")

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
            return None

        if resp.status_code >= 400:
            record_metric("upstream_errors", 1)
            logger.warning(
                "recall_resend_error",
                session_id=session_id, turn=turn_number, depth=depth, status=resp.status_code,
            )
            return None

        data = resp.json()

        # Check for second recall
        second_tc = find_recall_tool_call(data)
        if second_tc is None:
            # No more recall calls — done
            strip_recall_from_response(data)
            return data

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
            return data

        # Execute second recall
        second_recall_text = await handle_recall_tool_call(
            http_client=http_client,
            session_id=session_id,
            question=second_question,
            turn_number=turn_number,
        )

        # Build next message array for third request
        second_model_msg = data["choices"][0]["message"]
        second_remaining = [
            tc for tc in second_model_msg.get("tool_calls", [])
            if tc.get("function", {}).get("name") != RECALL_TOOL_NAME
        ]
        third_msg = dict(second_model_msg)
        if second_remaining:
            third_msg["tool_calls"] = second_remaining
        else:
            third_msg.pop("tool_calls", None)
        if not second_remaining and not third_msg.get("content"):
            third_msg["content"] = None

        current_messages = list(current_messages) + [third_msg, build_tool_result_message(
            second_tc.get("id", "recall_1"), second_recall_text,
        )]

    # Exhausted depth limit — return whatever we got
    strip_recall_from_response(data)
    return data


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
) -> dict[str, Any] | None:
    """Handle recall interception for a non-streaming response.

    If the response contains a __context_engine_recall call, intercept,
    execute the recall, and re-send. Returns the final response data,
    or None if no recall was detected (caller should use original response).
    """
    from src.proxy.tool_injection import find_recall_tool_call

    data = resp.json()
    tool_call = find_recall_tool_call(data)
    if tool_call is None:
        return None

    logger.info(
        "non_streaming_recall_intercepted",
        session_id=session_id, turn=turn_number,
    )
    get_metrics()["recall_tool_calls"] = get_metrics().get("recall_tool_calls", 0) + 1

    recall_text = await execute_recall(
        http_client=http_client,
        tool_call=tool_call,
        session_id=session_id,
        turn_number=turn_number,
    )
    if recall_text is None:
        return None

    model_message = data["choices"][0]["message"]
    resend_messages = build_resend_messages(
        original_messages=original_messages,
        model_message=model_message,
        tool_call=tool_call,
        recall_text=recall_text,
    )

    final_data = await resend_with_recall(
        http_client=http_client,
        url=url,
        headers=headers,
        original_body=body,
        resend_messages=resend_messages,
        session_id=session_id,
        turn_number=turn_number,
        max_retries=max_retries,
        backoff_base=backoff_base,
    )
    return final_data
