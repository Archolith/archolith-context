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
        self._request_start: float | None = None  # monotonic clock at request entry

    def set_request(
        self,
        session_id: str | None,
        turn_number: int,
        model: str,
        stream: bool,
        input_tokens: int,
        message_count: int,
        user_turn_count: int = 0,
        is_user_turn: bool = True,
    ) -> None:
        self._data["session_id"] = session_id
        self._data["turn_number"] = turn_number
        self._data["model"] = model
        self._data["stream"] = stream
        self._data["input_tokens"] = input_tokens
        self._data["message_count"] = message_count
        self._data["user_turn_count"] = user_turn_count
        self._data["is_user_turn"] = is_user_turn

    def set_token_telemetry(self, breakdown) -> None:
        """Record the structural token-accounting breakdown for this turn.

        Captures the input-side estimates (content / structural / client-reported /
        gate input) so the trace shows the true request size. Paired with the actual
        upstream prompt_tokens (set via set_response) this gives estimate vs actual.
        """
        gate_source = getattr(breakdown, "gate_source", None)
        self._data["token_content_est"] = breakdown.input_tokens_content_est
        self._data["token_structural_est"] = breakdown.input_tokens_structural_est
        self._data["token_client_reported"] = breakdown.input_tokens_client_reported
        self._data["token_gate_input"] = breakdown.gate_input_tokens
        self._data["token_gate_source"] = getattr(gate_source, "value", str(gate_source))
        self._data["token_estimator_version"] = breakdown.estimator_version

    def set_request_start(self, monotonic_start: float, wall_clock: float) -> None:
        """Record the monotonic clock at request entry for total latency calculation."""
        self._request_start = monotonic_start
        self._data["request_timestamp"] = wall_clock

    def set_filter_latency(self, filter_ms: float) -> None:
        """Record time spent in archolith-filter (per-request filter + agent-solo compression)."""
        self._data["filter_latency_ms"] = self._data.get("filter_latency_ms", 0.0) + filter_ms

    def finalize_timing(self, monotonic_now: float) -> None:
        """Compute total_latency_ms and proxy_overhead_ms from stored start time.

        Call this just before storing the trace, after upstream_latency_ms is set.
        """
        if self._request_start is not None:
            total = (monotonic_now - self._request_start) * 1000
            upstream = self._data.get("upstream_latency_ms", 0.0)
            self._data["total_latency_ms"] = round(total, 1)
            self._data["proxy_overhead_ms"] = round(max(0, total - upstream), 1)

    def set_original_messages(
        self,
        messages: list[dict],
        *,
        is_user_turn: bool = True,
    ) -> None:
        """Store original messages in the trace.

        On agent-solo turns (is_user_turn=False), skip the full message array
        and store only the count + last two messages.  The full array is
        monotonically growing and dominates trace storage (~99%); agent-solo
        turns just append 2-3 messages to the previous state.
        """
        # Set count unconditionally (both user and agent-solo turns)
        self._data["original_messages_count"] = len(messages)

        if not is_user_turn and len(messages) > 4:
            # Store lightweight summary: count + last 2 messages for debugging
            self._data["original_messages"] = _truncate_messages(messages[-2:])
        else:
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
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        prompt_tokens: int | None = None,
    ) -> None:
        self._data["upstream_status"] = status
        self._data["upstream_latency_ms"] = latency_ms
        self._data["output_tokens"] = output_tokens
        self._data["upstream_response_summary"] = response_summary[:500]
        self._data["cache_hit_tokens"] = cache_hit_tokens
        self._data["cache_miss_tokens"] = cache_miss_tokens
        # Actual upstream input tokens — pairs with token_structural_est for the
        # estimate-vs-actual reconciliation (TODO #8).
        self._data["prompt_tokens_actual"] = prompt_tokens

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
        trigger: str = "",
    ) -> None:
        self._data["recall_used"] = used
        self._data["recall_question"] = question[:200]
        self._data["recall_facts_returned"] = facts_returned
        if trigger:
            self._data["recall_trigger"] = trigger
        elif used:
            self._data["recall_trigger"] = "model_invoked"

    def set_fallback_reason(self, reason: str) -> None:
        self._data["fallback_reason"] = reason

    def set_filter_stats(
        self,
        available: bool,
        chars_saved: int = 0,
        chars_before: int = 0,
        chars_after: int = 0,
    ) -> None:
        """Record filter availability and per-turn char savings.

        Called after filter_request_body so the trace can distinguish:
        - filter_available=True  → package present and active
        - filter_available=False → package missing, filter failed open
        """
        self._data["filter_available"] = available
        self._data["filter_chars_saved"] = chars_saved
        self._data["filter_chars_before"] = chars_before
        self._data["filter_chars_after"] = chars_after
        self._data["outbound_chars_sent"] = chars_after
        if chars_saved > 0:
            self._data["filter_strategy_savings"] = {"request_filter": chars_saved}

    def set_outbound_context_stats(
        self,
        outbound_chars_sent: int,
        proxy_recall_chars_added: int = 0,
    ) -> None:
        """Record final outbound payload size after all proxy injections."""
        self._data["outbound_chars_sent"] = outbound_chars_sent
        self._data["proxy_recall_chars_added"] = max(0, proxy_recall_chars_added)

    def set_curator_skip_reason(self, reason: str) -> None:
        """Record why the curator was skipped or failed on this user turn.

        Called when the curator was eligible (session+graph ready, not over budget,
        is_user_turn) but curate_context returned None.  Values:
        - "cold_start"      — too few user turns to trigger curator
        - "disabled"        — curator_enabled or file_cache_enabled is False
        - "no_api_key"      — no curator/extractor API key configured
        - "timeout"         — curator exceeded latency budget
        - "no_result"       — curator loop produced no context block
        - "exception:..."   — unexpected exception in curator
        """
        self._data["curator_skip_reason"] = reason

    def set_solo_stats(self, stats: dict) -> None:
        """Record agent-solo compression strategy breakdown for trace inspection."""
        self._data["solo_strategies"] = stats.get("strategies_applied", [])
        self._data["solo_chars_saved_shrink"] = stats.get("chars_saved_shrink", 0)
        self._data["solo_chars_saved_dedup"] = stats.get("chars_saved_dedup", 0)
        self._data["solo_chars_saved_middle"] = stats.get("chars_saved_middle", 0)
        self._data["solo_chars_saved_compact"] = stats.get("chars_saved_compact", 0)
        self._data["solo_chars_saved_curator"] = stats.get("chars_saved_curator_cache", 0)
        self._data["solo_chars_saved_total"] = stats.get("total_chars_saved", 0)
        strategy_savings = dict(self._data.get("filter_strategy_savings") or {})
        for key, value in (
            ("curator_cache", stats.get("chars_saved_curator_cache", 0)),
            ("compact", stats.get("chars_saved_compact", 0)),
            ("middle_filter", stats.get("chars_saved_middle", 0)),
            ("dedup", stats.get("chars_saved_dedup", 0)),
            ("shrink", stats.get("chars_saved_shrink", 0)),
        ):
            if value > 0:
                strategy_savings[key] = value
        if strategy_savings:
            self._data["filter_strategy_savings"] = strategy_savings

    def set_curator_info(
        self,
        retained_turns: list[int] | None = None,
        context_block: str | None = None,
        tool_log: list[dict] | None = None,
        failure_reason: str = "",
        briefing_source_turn: int | None = None,
        briefing_chars: int = 0,
        briefing_files: int = 0,
    ) -> None:
        """Record the curator's turn-selection, context block, tool log, and failure reason for trace inspection."""
        self._data["curator_retained_turns"] = retained_turns
        # Cap context block at 4000 chars for trace storage
        if context_block:
            self._data["curator_context_block"] = context_block[:4000]
        if tool_log:
            self._data["curator_tool_log"] = tool_log
        if failure_reason:
            self._data["curator_failure_reason"] = failure_reason
        # Briefing metrics — set when assembly_mode is "briefing" or "briefing_stale"
        if briefing_source_turn is not None:
            self._data["briefing_source_turn"] = briefing_source_turn
            self._data["briefing_chars"] = briefing_chars
            self._data["briefing_files"] = briefing_files

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
