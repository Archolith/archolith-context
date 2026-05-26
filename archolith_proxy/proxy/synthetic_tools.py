"""Agent-initiated session summary tools.

Injects a synthetic tool definition into every request when a session is
active and synthetic_tools_enabled=true:

  recall_session_work()   -- structured summary of all work done this session

When the model calls the tool, the proxy intercepts the tool call, generates
a summary from the trace store and graph backend, and re-sends to upstream with
the synthetic result appended -- exactly like the __archolith_recall pattern.

The model never talks to an external service; the proxy owns the entire response.

Architecture:
1. inject_synthetic_tools(body) -- add tool def before forwarding upstream
2. Upstream responds with tool_calls containing the synthetic name
3. handle_non_streaming_synthetic() detects the call, generates the result, re-sends
4. strip_synthetic_tools / strip_synthetic_from_response clean up so client never
   sees internal tooling

Non-streaming path only (same limitation as __archolith_recall).
Gated behind SYNTHETIC_TOOLS_ENABLED=true (default false -- enable via .env).

Note: recall_file and recall_files_read have been superseded by transparent
native Read interception (proxy/tool_intercept.py). The file cache is now
served transparently when the model calls native Read tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# ── Tool names ─────────────────────────────────────────────────────────────────

SYNTHETIC_TOOL_NAMES = {"recall_session_work"}

# ── Tool definitions ───────────────────────────────────────────────────────────

_RECALL_SESSION_WORK_DEF = {
    "type": "function",
    "function": {
        "name": "recall_session_work",
        "description": (
            "Get a structured summary of all work completed in this session: "
            "files modified, decisions made, tests run, commands executed, and current state. "
            "Call this when you need to recall what has been done before writing a wrapup, "
            "commit message, or status report. Also useful when context window is filling up "
            "and you need a compact reference for prior work."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

SYNTHETIC_TOOL_DEFINITIONS = [_RECALL_SESSION_WORK_DEF]


# ── Injection / stripping ──────────────────────────────────────────────────────

def inject_synthetic_tools(body: dict[str, Any]) -> dict[str, Any]:
    """Inject synthetic tool definitions into the request body.

    Adds both tool definitions to body["tools"] if not already present.
    Idempotent -- safe to call multiple times.
    """
    tools = body.get("tools", [])

    # Check which are already present
    existing_names = {
        t.get("function", {}).get("name")
        for t in tools
        if isinstance(t, dict)
    }

    to_add = [
        defn for defn in SYNTHETIC_TOOL_DEFINITIONS
        if defn["function"]["name"] not in existing_names
    ]

    if to_add:
        body["tools"] = list(tools) + to_add
        logger.debug(
            "synthetic_tools_injected",
            added=[d["function"]["name"] for d in to_add],
            total_tools=len(body["tools"]),
        )

    return body


def strip_synthetic_tools(body: dict[str, Any]) -> dict[str, Any]:
    """Remove synthetic tool definitions from the request body.

    Called before re-sending so the model cannot call them again in the
    same turn (they are already satisfied via the intercepted tool result).
    """
    tools = body.get("tools", [])
    if not tools:
        return body

    filtered = [
        t for t in tools
        if not (
            isinstance(t, dict)
            and t.get("function", {}).get("name") in SYNTHETIC_TOOL_NAMES
        )
    ]

    if len(filtered) < len(tools):
        body["tools"] = filtered
        if not body["tools"]:
            del body["tools"]

    return body


def find_synthetic_tool_call(response_data: dict[str, Any]) -> dict | None:
    """Return the first synthetic tool call from a non-streaming response.

    Returns the tool_call dict (with id and function) if found, else None.
    """
    choices = response_data.get("choices", [])
    if not choices:
        return None

    message = choices[0].get("message", {})
    for tc in message.get("tool_calls", []):
        if (
            isinstance(tc, dict)
            and tc.get("function", {}).get("name") in SYNTHETIC_TOOL_NAMES
        ):
            return tc

    return None


def strip_synthetic_from_response(response_data: dict[str, Any]) -> dict[str, Any]:
    """Remove synthetic tool calls from the model's response.

    The client should not see internal proxy tools in the model's output.
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
            and tc.get("function", {}).get("name") in SYNTHETIC_TOOL_NAMES
        )
    ]

    if len(filtered) < len(tool_calls):
        message["tool_calls"] = filtered
        if not filtered:
            del message["tool_calls"]

    return response_data


# ── Summary generation ─────────────────────────────────────────────────────────

async def _generate_recall_session_work(session_id: str, turn_number: int) -> str:
    """Generate a structured work summary from trace store + graph backend."""
    from archolith_proxy.graph.backend import get_backend, is_graph_ready
    from archolith_proxy.trace.store import get_trace_store

    lines: list[str] = []
    lines.append(f"## Session Work Summary")

    # ── Trace store: turn metrics ────────────────────────────────────────
    trace_store = get_trace_store()
    try:
        turns = await trace_store.get_session_turns(session_id, limit=200)
    except Exception:
        turns = []

    user_turns = max((t.user_turn_count for t in turns), default=0)
    lines.append(f"**Turns:** {len(turns)} proxy turns, {user_turns} user turns\n")

    if not is_graph_ready():
        lines.append("*(Graph backend unavailable — full summary requires graph.)*")
        return "\n".join(lines)

    backend = get_backend()

    # ── Checkpoint (current state) ───────────────────────────────────────
    try:
        ckpt = await backend.get_checkpoint(session_id)
    except Exception:
        ckpt = None

    if ckpt:
        lines.append("### Current State")
        lines.append(ckpt.get("summary", ""))
        next_step = ckpt.get("next_step", "")
        if next_step:
            lines.append(f"**Next step:** {next_step}")
        lines.append("")

    # ── Files accessed ────────────────────────────────────────────────────
    try:
        cached_files = await backend.list_cached_files(session_id)
    except Exception:
        cached_files = []

    # Supplement with files_selected from trace turns (may include files not in cache)
    files_from_trace: dict[str, int] = {}  # path -> highest turn seen
    for t in turns:
        for f in (t.files_selected or []):
            p = f.get("path") or f.get("file") or ""
            if p:
                tn = t.turn_number
                if p not in files_from_trace or tn > files_from_trace[p]:
                    files_from_trace[p] = tn

    # Build unified file list
    cached_paths = {r.get("path", "") for r in cached_files}
    all_files: dict[str, dict] = {}
    for r in cached_files:
        p = r.get("path", "")
        if p:
            all_files[p] = {"path": p, "turn": r.get("last_updated_turn", 0)}
    for p, tn in files_from_trace.items():
        if p not in all_files:
            all_files[p] = {"path": p, "turn": tn}

    if all_files:
        lines.append("### Files Accessed")
        for info in sorted(all_files.values(), key=lambda x: x["turn"]):
            p = info["path"]
            t_num = info["turn"]
            tag = "(modified)" if p in cached_paths else "(read)"
            lines.append(f"- `{p}` {tag} (turn {t_num})")
        lines.append("")

    # ── Active facts (key observations + tool results) ────────────────────
    try:
        all_facts = await backend.get_active_facts(session_id, limit=100)
    except Exception:
        all_facts = []

    # Separate by type
    observations = [f for f in all_facts if f.get("fact_type") == "observation"]
    file_states = [f for f in all_facts if f.get("fact_type") == "file_state"]
    tool_results = [f for f in all_facts if f.get("fact_type") == "tool_result"]
    errors = [f for f in all_facts if f.get("fact_type") == "error"]

    if observations:
        lines.append("### Key Observations")
        for f in observations[:10]:  # Cap at 10 to avoid bloat
            src = f.get("source_turn", "?")
            content = (f.get("content") or "").strip()
            if content:
                lines.append(f"- (turn {src}) {content[:200]}")
        lines.append("")

    if file_states:
        lines.append("### File States")
        for f in file_states[:8]:
            src = f.get("source_turn", "?")
            content = (f.get("content") or "").strip()
            if content:
                lines.append(f"- (turn {src}) {content[:200]}")
        lines.append("")

    if tool_results:
        lines.append("### Tool Results")
        for f in tool_results[:6]:
            src = f.get("source_turn", "?")
            content = (f.get("content") or "").strip()
            if content:
                lines.append(f"- (turn {src}) {content[:200]}")
        lines.append("")

    if errors:
        lines.append("### Errors Encountered")
        for f in errors[:5]:
            src = f.get("source_turn", "?")
            content = (f.get("content") or "").strip()
            if content:
                lines.append(f"- (turn {src}) {content[:200]}")
        lines.append("")

    # ── Decisions ────────────────────────────────────────────────────────
    try:
        decisions = await backend.get_decisions(session_id)
    except Exception:
        decisions = []

    if decisions:
        lines.append("### Decisions Made")
        for d in decisions:
            summary = (d.get("summary") or "").strip()
            rationale = (d.get("rationale") or "").strip()
            turn = d.get("turn", "?")
            if summary:
                entry = f"- (turn {turn}) {summary}"
                if rationale:
                    entry += f" — {rationale[:100]}"
                lines.append(entry)
        lines.append("")

    # ── Recent turn summaries ─────────────────────────────────────────────
    recent_turns = [t for t in turns if t.upstream_response_summary]
    if recent_turns:
        lines.append("### Recent Activity")
        for t in recent_turns[-5:]:  # Last 5 substantive turns
            summary = (t.upstream_response_summary or "").strip()[:180]
            lines.append(f"- Turn {t.turn_number}: {summary}")
        lines.append("")

    if not lines[2:]:  # Only header lines added
        lines.append("*(No work recorded yet for this session.)*")

    return "\n".join(lines)


async def handle_synthetic_tool_call(
    session_id: str,
    tool_name: str,
    turn_number: int,
    args: dict | None = None,
) -> str:
    """Dispatch a synthetic tool call to its generator.

    Returns the formatted result string to inject as the tool result.
    """
    if tool_name == "recall_session_work":
        try:
            return await _generate_recall_session_work(session_id, turn_number)
        except Exception as e:
            logger.warning(
                "synthetic_tool_recall_work_failed",
                session_id=session_id, error=str(e), exc_info=True,
            )
            return f"Error generating session work summary: {e}"

    return f"Unknown synthetic tool: {tool_name}"


# ── Non-streaming interception ─────────────────────────────────────────────────

@dataclass
class SyntheticResult:
    """Result of a non-streaming synthetic tool interception."""

    final_data: dict[str, Any] | None
    synthetic_used: bool = False
    tool_name: str = ""
    fallback_used: bool = False  # True when re-send failed and fallback strip was applied


def _fallback_strip_synthetic(data: dict[str, Any]) -> SyntheticResult:
    """Return a SyntheticResult that strips synthetic tool calls from the model response.

    Used when a resend fails (error or timeout). Instead of returning the original
    response intact (which would expose synthetic tool calls to OpenCode and cause an
    infinite loop), strip the tool calls and normalise finish_reason to "stop".

    If non-synthetic tool calls remain after stripping, leave them intact so OpenCode
    can handle them normally. Only normalise finish_reason when the message is empty.
    """
    strip_synthetic_from_response(data)
    if data.get("choices"):
        choice = data["choices"][0]
        msg = choice.get("message", {})
        remaining_tool_calls = msg.get("tool_calls")
        if not remaining_tool_calls and not msg.get("content"):
            # Model only called synthetic tools — nothing useful remains.
            # Set a non-null content so the response is well-formed for OpenCode.
            msg["content"] = "(Session recall is temporarily unavailable. Continue your task without context recall — use file tools directly.)"
            choice["finish_reason"] = "stop"
        elif remaining_tool_calls:
            # Non-synthetic tool calls remain — preserve finish_reason=tool_calls
            # so OpenCode handles them normally.
            choice["finish_reason"] = "tool_calls"
    return SyntheticResult(final_data=data, synthetic_used=True)


async def handle_non_streaming_synthetic(
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
) -> SyntheticResult:
    """Handle synthetic tool interception for a non-streaming response.

    If the response contains a call to one of the synthetic tools, intercept
    it, generate the summary result, and re-send to upstream once with the
    tool result appended.

    Single re-send only (no multi-round; synthetic results are complete on
    first call and there is no need for iterative follow-up).

    Returns:
        SyntheticResult with final_data (None if no synthetic call was made),
        synthetic_used flag, and tool_name.
    """
    from archolith_proxy.proxy.upstream import upstream_request_with_retry
    from archolith_proxy.rtk import filter_request_body
    from archolith_proxy.config import get_settings

    data = resp.json()
    tool_call = find_synthetic_tool_call(data)

    if tool_call is None:
        return SyntheticResult(final_data=None, synthetic_used=False)

    tool_name = tool_call.get("function", {}).get("name", "")
    tool_call_id = tool_call.get("id", "synthetic_0")

    # Parse tool call arguments
    raw_args = tool_call.get("function", {}).get("arguments", "{}")
    try:
        tool_args: dict = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        tool_args = {}

    # Check for mixed tool calls: if model called non-synthetic tools alongside the
    # synthetic one, we cannot safely resend (the resend would only supply the
    # synthetic tool result, leaving the other tool_call_ids unanswered and causing
    # DeepSeek to return 400: "insufficient tool messages following tool_calls").
    model_message = data["choices"][0]["message"]
    all_tool_calls = model_message.get("tool_calls") or []
    non_synthetic_calls = [
        tc for tc in all_tool_calls
        if tc.get("function", {}).get("name") not in SYNTHETIC_TOOL_NAMES
    ]
    if non_synthetic_calls:
        # Mixed call — strip synthetic tool from response and let OpenCode handle
        # the non-synthetic tool calls normally.
        logger.info(
            "synthetic_tool_skipped_mixed_calls",
            session_id=session_id, turn=turn_number, tool=tool_name,
            non_synthetic=[tc.get("function", {}).get("name") for tc in non_synthetic_calls],
        )
        strip_synthetic_from_response(data)
        return SyntheticResult(final_data=data, synthetic_used=True)

    logger.info(
        "synthetic_tool_intercepted",
        session_id=session_id, turn=turn_number, tool=tool_name,
    )

    # Generate the summary result
    result_text = await handle_synthetic_tool_call(session_id, tool_name, turn_number, args=tool_args)

    # Build re-send messages:
    # original messages + model assistant message + synthetic tool result
    resend_messages = list(original_messages)
    resend_messages.append(dict(model_message))  # Keep tool_calls intact
    resend_messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result_text,
        "name": tool_name,
    })

    # Build re-send body: strip synthetic tool defs so the model can't loop
    settings = get_settings()
    body_dict = json.loads(body)
    strip_synthetic_tools(body_dict)

    resend_payload = {
        **body_dict,
        "stream": False,
        "messages": resend_messages,
    }
    resend_payload.pop("stream_options", None)  # stream_options is only valid with stream=true
    resend_payload = filter_request_body(resend_payload, enabled=settings.rtk_enabled)
    resend_body = json.dumps(resend_payload).encode("utf-8")

    try:
        resend_resp = await upstream_request_with_retry(
            client=http_client,
            url=url,
            headers=headers,
            content=resend_body,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        logger.warning(
            "synthetic_resend_failed",
            session_id=session_id, turn=turn_number, error=str(e),
        )
        # Strip synthetic tool calls from original response to prevent loop.
        # Returning raw model response with synthetic tool call would cause OpenCode
        # to handle it as an unknown tool, triggering another turn → infinite loop.
        result = _fallback_strip_synthetic(data)
        result.fallback_used = True
        return result

    if resend_resp.status_code >= 400:
        logger.warning(
            "synthetic_resend_error",
            session_id=session_id, turn=turn_number,
            status=resend_resp.status_code,
            error_body=resend_resp.text[:500],
        )
        # Same loop-prevention: strip synthetic tool calls from original response.
        result = _fallback_strip_synthetic(data)
        result.fallback_used = True
        return result

    final_data = resend_resp.json()
    # Strip any leftover synthetic tool calls from final response
    strip_synthetic_from_response(final_data)

    logger.info(
        "synthetic_tool_completed",
        session_id=session_id, turn=turn_number, tool=tool_name,
        result_length=len(result_text),
    )

    return SyntheticResult(
        final_data=final_data,
        synthetic_used=True,
        tool_name=tool_name,
    )
