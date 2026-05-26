"""Transparent native Read tool call interception.

When the LLM calls a native file-read tool (Read, read_file, etc.), this module
checks the session's file content cache. If every requested file is cached, the
intercepted results are injected as tool-result messages and re-sent to upstream
transparently — the agent gets the same response without a disk read or extra RTT.

No model cooperation required: the model calls Read normally and gets cache results.

Architecture:
1. handle_native_read_intercept() inspects the model's tool_calls
2. If ALL calls are Read-like AND ALL files are cached → inject results → re-send
3. If ANY call is non-Read or ANY file is a cache miss → pass through normally

Phase 1 is all-or-nothing: partial batch interception (some hits, some misses)
is deferred to Phase 2 to avoid the complexity of mixed synthetic/real tool results.

Requires synthetic_tools_enabled=True (which forces stream=False for all requests,
giving us access to the full non-streaming response before forwarding).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

# ── Interceptable tool names ───────────────────────────────────────────────────

NATIVE_INTERCEPTABLE_TOOLS: frozenset[str] = frozenset({
    "Read", "read_file", "read", "ReadFile",
})

# Write/edit tools that invalidate the file cache.
# If any of these appear in the inbound messages, the current turn may have
# modified files that are cached — skip interception entirely so the model
# always gets a fresh read from disk. Invalidation runs in the background
# extraction task; the intercept must not race against it.
NATIVE_WRITE_TOOLS: frozenset[str] = frozenset({
    "Write", "write", "write_file",
    "Edit", "edit", "edit_file",
    "create_file", "create",
    "patch", "str_replace_editor", "str_replace_based_edit_tool",
})

# Known argument keys for the file path in Read-like tools
_PATH_ARG_KEYS = ("file_path", "path", "filePath", "filename", "target_file")

# Known argument keys for offset/limit (line-range reads)
_OFFSET_ARG_KEYS = ("offset", "start_line", "start")
_LIMIT_ARG_KEYS = ("limit", "end_line", "end", "line_count")


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class NativeInterceptResult:
    """Result of a native Read tool call interception attempt."""

    final_data: dict[str, Any] | None  # None = not intercepted (pass through)
    intercepted: bool = False
    files_served_from_cache: list[str] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_read_args(args: dict) -> tuple[str, int | None, int | None]:
    """Extract (path, offset, limit) from a Read tool call's arguments.

    offset/limit are None when not provided (full-file read).
    """
    path = ""
    for key in _PATH_ARG_KEYS:
        if key in args and args[key]:
            path = str(args[key])
            break

    offset = None
    for key in _OFFSET_ARG_KEYS:
        if key in args and args[key] is not None:
            offset = int(args[key])
            break

    limit = None
    for key in _LIMIT_ARG_KEYS:
        if key in args and args[key] is not None:
            limit = int(args[key])
            break

    return path, offset, limit


async def _format_cached_file(
    session_id: str,
    path: str,
    offset: int | None,
    limit: int | None,
) -> str | None:
    """Format cached file content to match the Read tool output exactly.

    Returns None if the file is not in cache.
    Output format reproduces the Read tool's numbered-line format:
        1: line one
        2: line two
        ...
    """
    from archolith_proxy.graph.backend import get_backend, is_graph_ready

    if not is_graph_ready():
        return None

    backend = get_backend()

    if offset is not None or limit is not None:
        # Line-range read
        start = max(1, offset or 1)
        end = start + (limit or 2000) - 1  # Default large range if only offset given

        # Get the full file info to clamp end to line_count
        file_info = await backend.get_file_content(session_id, path)
        if not file_info:
            return None

        line_count = file_info.get("line_count", 0)
        end = min(end, line_count)

        result = await backend.get_file_lines(session_id, path, start, end)
        if not result:
            return None
        return result
    else:
        # Full-file read
        file_info = await backend.get_file_content(session_id, path)
        if not file_info:
            return None

        content = file_info.get("content", "")
        lines = content.split("\n")
        numbered = [f"{i + 1}: {line}" for i, line in enumerate(lines)]
        return "\n".join(numbered)


# ── Main intercept handler ─────────────────────────────────────────────────────

async def handle_native_read_intercept(
    resp: httpx.Response,
    http_client: httpx.AsyncClient,
    url: str,
    headers: dict,
    body: bytes,
    session_id: str,
    turn_number: int,
    original_messages: list[dict],
) -> NativeInterceptResult:
    """Intercept native Read calls and serve from file cache when all are hits.

    Returns NativeInterceptResult with:
    - final_data = None: not intercepted (pass through to client normally)
    - final_data = dict: intercepted, re-send succeeded (use this as the response)
    """
    from archolith_proxy.config import get_settings
    from archolith_proxy.graph.backend import is_graph_ready
    from archolith_proxy.metrics import record_metric
    from archolith_proxy.proxy.upstream import upstream_request_with_retry
    from archolith_proxy.rtk import filter_request_body

    settings = get_settings()

    # Gate: both synthetic_tools_enabled and native_read_intercept_enabled required
    if not settings.synthetic_tools_enabled or not settings.native_read_intercept_enabled:
        return NativeInterceptResult(final_data=None)

    # Gate: graph backend must be ready for cache lookups
    if not is_graph_ready():
        return NativeInterceptResult(final_data=None)

    # Gate: if the IMMEDIATELY PRECEDING assistant turn contained a write/edit
    # tool call, skip interception. The extraction background task that
    # re-caches the written file runs after the response is returned — if we
    # intercept in the very next turn the cache may still be stale.
    # We only check the last assistant message, not all of history: earlier
    # writes have already been processed and the cache is fresh again.
    last_assistant: dict | None = None
    for msg in original_messages:
        if msg.get("role") == "assistant":
            last_assistant = msg
    if last_assistant is not None:
        for tc in (last_assistant.get("tool_calls") or []):
            tc_name = tc.get("function", {}).get("name", "")
            if tc_name in NATIVE_WRITE_TOOLS:
                logger.debug(
                    "native_read_intercept_skipped_write_in_history",
                    session_id=session_id, turn=turn_number, write_tool=tc_name,
                )
                return NativeInterceptResult(final_data=None)

    # Parse the model's response
    try:
        data = resp.json()
    except Exception:
        return NativeInterceptResult(final_data=None)

    choices = data.get("choices", [])
    if not choices:
        return NativeInterceptResult(final_data=None)

    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls") or []

    if not tool_calls:
        return NativeInterceptResult(final_data=None)

    # ── Step 1: Classify all tool calls ────────────────────────────────────

    read_calls: list[tuple[dict, str, int | None, int | None]] = []  # (tc, path, offset, limit)
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "")

        if name not in NATIVE_INTERCEPTABLE_TOOLS:
            # Non-Read tool call present → all-or-nothing: pass through
            return NativeInterceptResult(final_data=None)

        # Parse arguments
        raw_args = func.get("arguments", "{}")
        try:
            args: dict = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}

        path, offset, limit = _extract_read_args(args)
        if not path:
            # Can't intercept without a path → pass through
            return NativeInterceptResult(final_data=None)

        read_calls.append((tc, path, offset, limit))

    # ── Step 2: Check cache for all files ──────────────────────────────────

    tool_results: list[dict[str, Any]] = []
    files_served: list[str] = []

    for tc, path, offset, limit in read_calls:
        formatted = await _format_cached_file(session_id, path, offset, limit)
        if formatted is None:
            # Cache miss → all-or-nothing: pass through
            record_metric("native_read_cache_misses", 1)
            logger.debug(
                "native_read_cache_miss",
                session_id=session_id, turn=turn_number,
                path=path,
            )
            return NativeInterceptResult(final_data=None)

        # Cache hit — build tool result message
        tool_results.append({
            "role": "tool",
            "tool_call_id": tc.get("id", ""),
            "content": formatted,
            "name": tc.get("function", {}).get("name", "Read"),
        })
        files_served.append(path)

    # ── Step 3: All hits — build re-send ───────────────────────────────────

    record_metric("native_read_cache_hits", len(files_served))

    logger.info(
        "native_read_intercept",
        session_id=session_id,
        turn=turn_number,
        files=files_served,
    )

    # Build re-send messages: original + assistant message + tool results
    resend_messages = list(original_messages)
    resend_messages.append(dict(message))  # Keep model's tool_calls intact

    for tool_result in tool_results:
        resend_messages.append(tool_result)

    # Build re-send body
    body_dict = json.loads(body)
    # No need to strip synthetic tools here — we're intercepting native reads,
    # not synthetic calls. The tools array stays as-is.
    resend_payload = {
        **body_dict,
        "stream": False,
        "messages": resend_messages,
    }
    resend_payload.pop("stream_options", None)
    resend_payload = filter_request_body(resend_payload, enabled=settings.rtk_enabled)
    resend_body = json.dumps(resend_payload).encode("utf-8")

    # ── Step 4: Re-send to upstream ────────────────────────────────────────

    try:
        resend_resp = await upstream_request_with_retry(
            client=http_client,
            url=url,
            headers=headers,
            content=resend_body,
            max_retries=settings.upstream_max_retries,
            backoff_base=settings.upstream_retry_backoff_base_s,
        )
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        logger.warning(
            "native_read_resend_failed",
            session_id=session_id, turn=turn_number, error=str(e),
        )
        record_metric("native_read_intercept_errors", 1)
        # Safe fallback: return None so the original response passes through
        return NativeInterceptResult(final_data=None)

    if resend_resp.status_code >= 400:
        logger.warning(
            "native_read_resend_error",
            session_id=session_id, turn=turn_number,
            status=resend_resp.status_code,
            error_body=resend_resp.text[:500],
        )
        record_metric("native_read_intercept_errors", 1)
        # Safe fallback: return None so the original response passes through
        return NativeInterceptResult(final_data=None)

    final_data = resend_resp.json()

    logger.info(
        "native_read_intercept_completed",
        session_id=session_id,
        turn=turn_number,
        files_served=files_served,
    )

    return NativeInterceptResult(
        final_data=final_data,
        intercepted=True,
        files_served_from_cache=files_served,
    )
