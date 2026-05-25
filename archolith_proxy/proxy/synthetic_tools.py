"""Agent-initiated session summary tools.

Injects two synthetic tool definitions into every request when a session is
active and synthetic_tools_enabled=true:

  recall_session_work()   -- structured summary of all work done this session
  recall_files_read()     -- list of files accessed, to skip redundant re-reads

When the model calls either tool, the proxy intercepts the tool call, generates
a summary from the trace store and graph backend, and re-sends to upstream with
the synthetic result appended -- exactly like the __archolith_recall pattern.

The model never talks to an external service; the proxy owns the entire response.

Architecture:
1. inject_synthetic_tools(body) -- add two tool defs before forwarding upstream
2. Upstream responds with tool_calls containing one of the synthetic names
3. handle_non_streaming_synthetic() detects the call, generates the result, re-sends
4. strip_synthetic_tools / strip_synthetic_from_response clean up so client never
   sees internal tooling

Non-streaming path only (same limitation as __archolith_recall).
Gated behind SYNTHETIC_TOOLS_ENABLED=true (default false -- enable via .env).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# ── Tool names ─────────────────────────────────────────────────────────────────

SYNTHETIC_TOOL_NAMES = {"recall_session_work", "recall_files_read", "recall_file"}

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

_RECALL_FILES_READ_DEF = {
    "type": "function",
    "function": {
        "name": "recall_files_read",
        "description": (
            "Get the list of files that have been read or accessed in this session. "
            "Use this to avoid re-reading files you already have context for. "
            "Returns each file path with status (read/modified/created) and the turn "
            "it was last accessed. If a file appears here, you have recent context on it "
            "unless you specifically need updated content."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

_RECALL_FILE_DEF = {
    "type": "function",
    "function": {
        "name": "recall_file",
        "description": (
            "Retrieve cached file content from session memory. "
            "Use this instead of re-reading files from disk after context compression. "
            "Provide start_line and end_line for targeted retrieval. "
            "Without a line range, returns a 10-line preview and the total line count."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path exactly as previously read in this session.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-indexed start line (inclusive).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-indexed end line (inclusive).",
                },
            },
            "required": ["path"],
        },
    },
}

SYNTHETIC_TOOL_DEFINITIONS = [_RECALL_SESSION_WORK_DEF, _RECALL_FILES_READ_DEF, _RECALL_FILE_DEF]


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


async def _generate_recall_files_read(session_id: str, turn_number: int) -> str:
    """Generate a list of files accessed this session."""
    from archolith_proxy.graph.backend import get_backend, is_graph_ready
    from archolith_proxy.trace.store import get_trace_store

    lines: list[str] = ["## Files Accessed This Session"]

    if not is_graph_ready():
        lines.append("*(Graph backend unavailable.)*")
        return "\n".join(lines)

    backend = get_backend()

    # Primary source: file content cache (records actual file reads/writes)
    try:
        cached_files = await backend.list_cached_files(session_id)
    except Exception:
        cached_files = []

    # Supplement with files_selected from trace store (assembler-selected files)
    trace_store = get_trace_store()
    try:
        turns = await trace_store.get_session_turns(session_id, limit=200)
    except Exception:
        turns = []

    # Build a map: path -> {turn, in_cache, line_count}
    file_map: dict[str, dict] = {}
    for r in cached_files:
        p = r.get("path", "")
        if p:
            file_map[p] = {
                "path": p,
                "turn": r.get("last_updated_turn", 0),
                "line_count": r.get("line_count"),
                "cached": True,
            }

    for t in turns:
        for f in (t.files_selected or []):
            p = f.get("path") or f.get("file") or ""
            if p and p not in file_map:
                file_map[p] = {
                    "path": p,
                    "turn": t.turn_number,
                    "line_count": None,
                    "cached": False,
                }

    if not file_map:
        lines.append("*(No files have been accessed yet.)*")
        return "\n".join(lines)

    # Sort by turn ascending
    sorted_files = sorted(file_map.values(), key=lambda x: x["turn"])

    for info in sorted_files:
        p = info["path"]
        tn = info["turn"]
        lc = info["line_count"]
        cached = info["cached"]
        tag = "cached" if cached else "referenced"
        lc_str = f", {lc} lines" if lc else ""
        lines.append(f"- `{p}` (turn {tn}, {tag}{lc_str})")

    lines.append(f"\n**Total:** {len(file_map)} files")
    return "\n".join(lines)


async def _generate_recall_file(
    session_id: str,
    path: str,
    start_line: int | None,
    end_line: int | None,
) -> str:
    """Return cached file content for the requested path and optional line range.

    Behavior:
    - Cache miss: plain message telling the agent to read normally.
    - No range: 10-line preview + total line count + hint to use a range.
    - Range provided: numbered lines clamped to recall_file_max_lines.
    """
    from archolith_proxy.config import get_settings
    from archolith_proxy.graph.backend import get_backend, is_graph_ready

    if not path:
        return "(recall_file: path is required)"

    if not is_graph_ready():
        return "(recall_file: graph backend unavailable — cannot retrieve cached file)"

    settings = get_settings()
    backend = get_backend()

    file_info = await backend.get_file_content(session_id, path)
    if not file_info:
        return f"(not cached: {path} — read it normally)"

    line_count = file_info.get("line_count", 0)
    sha_prefix = (file_info.get("sha256") or "")[:8]

    # No range: preview + metadata only
    if start_line is None and end_line is None:
        preview_end = min(10, line_count)
        preview = await backend.get_file_lines(session_id, path, 1, preview_end) or ""
        return (
            f"{path} — {line_count} lines (sha256:{sha_prefix})\n\n"
            f"{preview}\n\n"
            f"[{line_count} lines total — provide start_line/end_line to retrieve a range]"
        )

    # Range recall — enforce max_lines cap
    start = max(1, start_line or 1)
    raw_end = end_line or (start + settings.recall_file_max_lines - 1)
    end = min(raw_end, start + settings.recall_file_max_lines - 1)

    result = await backend.get_file_lines(session_id, path, start, end)
    if not result:
        return f"(no content in range {start}–{end} for {path})"

    actual_end = min(end, line_count)
    was_clamped = (end_line is not None) and (end < end_line)
    header = f"{path} lines {start}–{actual_end} of {line_count} (sha256:{sha_prefix})"
    if was_clamped:
        header += f" [clamped to {settings.recall_file_max_lines} lines — request next range to continue]"
    return f"{header}\n\n{result}"


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

    elif tool_name == "recall_files_read":
        try:
            return await _generate_recall_files_read(session_id, turn_number)
        except Exception as e:
            logger.warning(
                "synthetic_tool_recall_files_failed",
                session_id=session_id, error=str(e), exc_info=True,
            )
            return f"Error generating files-read list: {e}"

    elif tool_name == "recall_file":
        call_args = args or {}
        path = call_args.get("path", "")
        start_line = call_args.get("start_line")
        end_line = call_args.get("end_line")
        try:
            return await _generate_recall_file(session_id, path, start_line, end_line)
        except Exception as e:
            logger.warning(
                "synthetic_tool_recall_file_failed",
                session_id=session_id, path=path, error=str(e), exc_info=True,
            )
            return f"Error retrieving cached file: {e}"

    return f"Unknown synthetic tool: {tool_name}"


# ── Non-streaming interception ─────────────────────────────────────────────────

@dataclass
class SyntheticResult:
    """Result of a non-streaming synthetic tool interception."""

    final_data: dict[str, Any] | None
    synthetic_used: bool = False
    tool_name: str = ""


def _fallback_strip_synthetic(data: dict[str, Any]) -> SyntheticResult:
    """Return a SyntheticResult that strips synthetic tool calls from the model response.

    Used when a resend fails (error or timeout). Instead of returning the original
    response intact (which would expose synthetic tool calls to OpenCode and cause an
    infinite loop), strip the tool calls and normalise finish_reason to "stop".
    """
    strip_synthetic_from_response(data)
    # If the message now has no tool_calls and no content (model only called synthetic
    # tools), set finish_reason="stop" and ensure content is a non-null empty string
    # so the response is well-formed for OpenCode.
    if data.get("choices"):
        choice = data["choices"][0]
        msg = choice.get("message", {})
        if not msg.get("tool_calls") and not msg.get("content"):
            msg["content"] = ""
            choice["finish_reason"] = "stop"
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

    # Parse tool call arguments (present for parameterized tools like recall_file)
    raw_args = tool_call.get("function", {}).get("arguments", "{}")
    try:
        tool_args: dict = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        tool_args = {}

    logger.info(
        "synthetic_tool_intercepted",
        session_id=session_id, turn=turn_number, tool=tool_name,
    )

    # Generate the summary result
    result_text = await handle_synthetic_tool_call(session_id, tool_name, turn_number, args=tool_args)

    # Build re-send messages:
    # original messages + model assistant message + synthetic tool result
    model_message = data["choices"][0]["message"]
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
        return _fallback_strip_synthetic(data)

    if resend_resp.status_code >= 400:
        logger.warning(
            "synthetic_resend_error",
            session_id=session_id, turn=turn_number,
            status=resend_resp.status_code,
            error_body=resend_resp.text[:500],
        )
        # Same loop-prevention: strip synthetic tool calls from original response.
        return _fallback_strip_synthetic(data)

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
