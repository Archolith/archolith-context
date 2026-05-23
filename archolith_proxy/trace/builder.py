"""Trace builder — incremental construction of TurnTrace during proxy handling.

The TurnTrace is built across multiple phases of a single request:
1. Request arrival: session_id, turn_number, model, stream, input_tokens, message_count
2. Assembly: mode, reason, latency, facts/files/decisions selected, rewritten tokens, savings
3. Upstream response: status, latency, output_tokens, response summary
4. Extraction: facts_stored, duplicates_skipped, invalidations, latency
5. Recall: recall_used, question, facts_returned

The builder accumulates data and the caller calls .build() at the end.
Extraction data is filled in by a post-extraction callback since extraction
runs as a background task.
"""

from __future__ import annotations

import copy

from archolith_proxy.models.dtos import TurnTrace


class TraceBuilder:
    """Incremental builder for a TurnTrace record.

    Usage:
        builder = TraceBuilder()
        builder.set_request(session_id, turn_number, model, stream, input_tokens, message_count)
        builder.set_original_messages(messages)
        builder.set_assembly(mode, reason, latency_ms, ...)
        builder.set_response(status, latency_ms, output_tokens, summary)
        trace = builder.build()
        # Later, after extraction:
        builder.set_extraction(facts_stored, duplicates_skipped, ...)
        trace = builder.build()  # Re-build with extraction data
    """

    def __init__(self) -> None:
        self._data: dict = {}

    def set_request(
        self,
        session_id: str | None,
        turn_number: int,
        model: str,
        stream: bool,
        input_tokens: int,
        message_count: int,
        user_turn_count: int = 0,
    ) -> None:
        self._data["session_id"] = session_id
        self._data["turn_number"] = turn_number
        self._data["model"] = model
        self._data["stream"] = stream
        self._data["input_tokens"] = input_tokens
        self._data["message_count"] = message_count
        self._data["user_turn_count"] = user_turn_count

    def set_original_messages(self, messages: list[dict]) -> None:
        # Deep copy to avoid mutation issues; truncate very long messages
        self._data["original_messages"] = _truncate_messages(messages)

    def set_rewritten_messages(self, messages: list[dict]) -> None:
        self._data["rewritten_messages"] = _truncate_messages(messages)

    def set_assembly(
        self,
        mode: str,
        reason: str = "",
        latency_ms: float = 0.0,
        facts_selected: list[dict] | None = None,
        files_selected: list[dict] | None = None,
        decisions_selected: list[dict] | None = None,
        rewritten_tokens: int = 0,
        savings_tokens: int = 0,
        savings_ratio: float = 0.0,
        compression_ratio: float = 1.0,
    ) -> None:
        self._data["assembly_mode"] = mode
        self._data["assembly_reason"] = reason
        self._data["assembly_latency_ms"] = latency_ms
        self._data["facts_selected"] = facts_selected or []
        self._data["files_selected"] = files_selected or []
        self._data["decisions_selected"] = decisions_selected or []
        self._data["rewritten_tokens"] = rewritten_tokens
        self._data["savings_tokens"] = savings_tokens
        self._data["savings_ratio"] = savings_ratio
        self._data["compression_ratio"] = compression_ratio

    def set_response(
        self,
        status: int,
        latency_ms: float = 0.0,
        output_tokens: int | None = None,
        response_summary: str = "",
    ) -> None:
        self._data["upstream_status"] = status
        self._data["upstream_latency_ms"] = latency_ms
        self._data["output_tokens"] = output_tokens
        self._data["upstream_response_summary"] = response_summary[:500]

    def set_extraction(
        self,
        facts_stored: int = 0,
        duplicates_skipped: int = 0,
        invalidations_attempted: int = 0,
        invalidations_matched: int = 0,
        extraction_latency_ms: float = 0.0,
        extracted_facts: list[dict] | None = None,
    ) -> None:
        self._data["facts_stored"] = facts_stored
        self._data["duplicates_skipped"] = duplicates_skipped
        self._data["invalidations_attempted"] = invalidations_attempted
        self._data["invalidations_matched"] = invalidations_matched
        self._data["extraction_latency_ms"] = extraction_latency_ms
        self._data["extracted_facts"] = extracted_facts or []

    def set_recall(
        self,
        used: bool = True,
        question: str = "",
        facts_returned: int = 0,
    ) -> None:
        self._data["recall_used"] = used
        self._data["recall_question"] = question[:200]
        self._data["recall_facts_returned"] = facts_returned

    def set_fallback_reason(self, reason: str) -> None:
        self._data["fallback_reason"] = reason

    def build(self) -> TurnTrace:
        """Build the TurnTrace from accumulated data."""
        return TurnTrace(**self._data)


# Max content length per message to keep traces bounded
_MAX_MESSAGE_CONTENT = 2000


def _truncate_messages(messages: list[dict]) -> list[dict]:
    """Deep-copy messages and truncate long content for trace storage.

    Preserves structure (role, tool_calls, etc.) but limits content
    strings to _MAX_MESSAGE_CONTENT chars to bound memory usage.
    """
    result = []
    for msg in messages:
        copy_msg = {}
        for k, v in msg.items():
            if k == "content" and isinstance(v, str) and len(v) > _MAX_MESSAGE_CONTENT:
                copy_msg[k] = v[:_MAX_MESSAGE_CONTENT] + f"... [{len(v)} chars truncated]"
            elif k == "content" and isinstance(v, list):
                # Multi-part content
                copy_msg[k] = _truncate_multipart(v)
            elif k == "tool_calls" and isinstance(v, list):
                copy_msg[k] = _truncate_tool_calls(v)
            else:
                copy_msg[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
        result.append(copy_msg)
    return result


def _truncate_multipart(parts: list) -> list:
    """Truncate multi-part message content."""
    result = []
    for part in parts:
        if isinstance(part, dict):
            p = dict(part)
            text = p.get("text", "")
            if isinstance(text, str) and len(text) > _MAX_MESSAGE_CONTENT:
                p["text"] = text[:_MAX_MESSAGE_CONTENT] + f"... [{len(text)} chars truncated]"
            result.append(p)
        else:
            result.append(part)
    return result


def _truncate_tool_calls(tool_calls: list) -> list:
    """Truncate tool call arguments for trace storage."""
    result = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            copy_tc = dict(tc)
            func = copy_tc.get("function", {})
            if isinstance(func, dict):
                args = func.get("arguments", "")
                if isinstance(args, str) and len(args) > _MAX_MESSAGE_CONTENT:
                    copy_tc["function"] = {
                        **func,
                        "arguments": args[:_MAX_MESSAGE_CONTENT] + f"... [{len(args)} chars truncated]",
                    }
            result.append(copy_tc)
        else:
            result.append(tc)
    return result
