"""Helper utilities extracted from chat.py — message normalization, tool call tracking, file read extraction."""

from __future__ import annotations

import json

import structlog

from archolith_proxy.models.graph_nodes import FileStatus
from archolith_proxy.filter_adapter import filter_single_tool_result

__all__ = [
    "_normalize_message_content",
    "_build_call_map",
    "_extract_tool_path",
    "_prefer_stronger_file_status",
    "_infer_file_touch_statuses",
    "_extract_file_reads",
    "_extract_user_message",
    "_collect_recent_tool_results",
    "_collect_tool_call_records",
    "_extract_finish_reason",
    "_extract_response_text",
]

logger = structlog.get_logger()


def _normalize_message_content(content: object) -> str:
    """Flatten OpenAI-style message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = _normalize_message_content(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if nested is not None:
            return _normalize_message_content(nested)
    return ""


def _build_call_map(messages: list[dict]) -> dict[str, tuple[str, dict]]:
    """Build tool_call_id → (tool_name, args) lookup from all assistant messages.

    Shared utility used by _extract_file_reads, _extract_file_writes,
    and _collect_tool_call_records to avoid duplicating the call_map
    construction pattern.
    """
    call_map: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in (msg.get("tool_calls") or []):
            try:
                args = json.loads(tc["function"]["arguments"])
            except (KeyError, json.JSONDecodeError):
                args = {}
            call_map[tc.get("id", "")] = (tc["function"]["name"], args)
    return call_map


def _extract_tool_path(args: dict) -> str:
    """Extract a file path from common tool argument shapes."""
    return (
        args.get("path") or args.get("file_path")
        or args.get("filePath") or args.get("filename")
        or args.get("target_file") or ""
    )


_READ_ONLY_TOOL_NAMES = frozenset({
    "read",
    "grep",
    "glob",
    "find",
    "findfiles",
    "find_files",
    "ls",
    "list_directory",
    "listdir",
    "webfetch",
    "web_fetch",
    "fetch",
})
_MODIFIED_TOOL_NAMES = frozenset({"write", "edit", "notebookedit", "write_file", "edit_file"})
_CREATED_TOOL_NAMES = frozenset({"create", "create_file"})
_DELETED_TOOL_NAMES = frozenset({"delete", "delete_file", "remove_file"})
_FILE_STATUS_PRIORITY = {
    FileStatus.READ: 1,
    FileStatus.MODIFIED: 2,
    FileStatus.CREATED: 3,
    FileStatus.DELETED: 4,
}


def _prefer_stronger_file_status(
    current: FileStatus | None,
    candidate: FileStatus,
) -> FileStatus:
    """Keep the strongest status seen for a path within the current turn."""
    if current is None:
        return candidate
    if _FILE_STATUS_PRIORITY[candidate] >= _FILE_STATUS_PRIORITY[current]:
        return candidate
    return current


def _infer_file_touch_statuses(messages: list[dict]) -> tuple[dict[str, FileStatus], FileStatus]:
    """Infer read vs write-style file touches from current-turn tool calls.

    Returns a per-path status map plus a fallback status for files_touched
    entries that the extractor found without an exact tool-argument path match.
    """
    last_assistant: dict | None = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_assistant = msg
            break

    if not last_assistant:
        return {}, FileStatus.MODIFIED

    statuses: dict[str, FileStatus] = {}
    current_turn_tool_names: set[str] = set()

    for tc in last_assistant.get("tool_calls") or []:
        function = tc.get("function", {})
        tool_name = str(function.get("name", ""))
        normalized_name = tool_name.casefold()
        current_turn_tool_names.add(normalized_name)

        try:
            args = json.loads(function.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}

        path = _extract_tool_path(args)
        if not path:
            continue

        if normalized_name in _CREATED_TOOL_NAMES:
            candidate = FileStatus.CREATED
        elif normalized_name in _DELETED_TOOL_NAMES:
            candidate = FileStatus.DELETED
        elif normalized_name in _MODIFIED_TOOL_NAMES:
            candidate = FileStatus.MODIFIED
        elif normalized_name in _READ_ONLY_TOOL_NAMES:
            candidate = FileStatus.READ
        else:
            continue

        statuses[path] = _prefer_stronger_file_status(statuses.get(path), candidate)

    has_write_like_tool = any(
        tool_name in _CREATED_TOOL_NAMES
        or tool_name in _DELETED_TOOL_NAMES
        or tool_name in _MODIFIED_TOOL_NAMES
        for tool_name in current_turn_tool_names
    )
    fallback_status = FileStatus.MODIFIED if has_write_like_tool else FileStatus.READ
    return statuses, fallback_status


def _collect_tool_call_records(messages: list[dict]) -> list:
    """Build ToolCallRecord list from the CURRENT TURN's tool calls only.

    "Current turn" = tool messages paired with the most recent assistant message
    that has tool_calls. Scoped to this turn to avoid re-processing tool calls
    from previous turns (which have already been extracted and stored).

    Applies RTK Layer 1 filter — the messages array passed to extraction is the
    ORIGINAL (pre-rewrite) array; the outbound RTK filter runs on a copy in
    filter_request_body() and does not mutate the source array.
    """
    from archolith_proxy.extractor.base import ToolCallRecord

    # Find the most recent assistant message with tool_calls — that defines the current turn.
    # Older turns' tool results have already been extracted in prior calls.
    last_assistant: dict | None = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_assistant = msg
            break

    if not last_assistant:
        return []

    # Build id → (name, args) from the current-turn assistant message only
    current_turn_map: dict[str, tuple[str, dict]] = {}
    for tc in (last_assistant.get("tool_calls") or []):
        try:
            args = json.loads(tc["function"]["arguments"])
        except (KeyError, json.JSONDecodeError):
            args = {}
        current_turn_map[tc.get("id", "")] = (tc["function"]["name"], args)

    records = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tc_id = msg.get("tool_call_id", "")
        if tc_id not in current_turn_map:
            continue
        tool_name, args = current_turn_map[tc_id]
        content = _normalize_message_content(msg.get("content", ""))
        # Apply RTK Layer 1 filter — same as _collect_recent_tool_results()
        content = filter_single_tool_result(content, tool_name=tool_name)
        records.append(ToolCallRecord(
            tool_call_id=tc_id,
            tool_name=tool_name,
            args=args,
            result=content,
        ))
    return records  # all results from current turn, no cap — extractors size-limit individually


def _extract_response_text(response_data: dict) -> str:
    """Extract assistant text from a non-streaming chat completion response."""
    choices = response_data.get("choices", [])
    if not choices:
        return ""
    message = choices[0].get("message", {})
    return _normalize_message_content(message.get("content"))


def _extract_finish_reason(response_data: dict) -> str | None:
    """Extract finish_reason from the first choice of a non-streaming response."""
    choices = response_data.get("choices", [])
    if not choices:
        return None
    return choices[0].get("finish_reason")


def _extract_file_reads(messages: list[dict]) -> list[dict]:
    """Pair file-read tool calls with their results via tool_call_id.

    Iterates the messages array to build a lookup from assistant tool_calls,
    then matches tool result messages by tool_call_id. Only returns pairs
    where the tool is NOT a compressible tool (i.e., it's a file-read tool)
    and content is a non-empty string.

    Returns list of {path, content, tool_call_id, tool_name}.
    """
    from archolith_proxy.proxy.rewrite import _is_compressible_tool

    call_map = _build_call_map(messages)

    # Debug: log message structure when call_map is empty to diagnose extraction misses
    if logger.isEnabledFor(__import__('logging').DEBUG):
        role_counts = {}
        for m in messages:
            r = m.get("role", "unknown")
            role_counts[r] = role_counts.get(r, 0) + 1
        tool_msg_ids = [m.get("tool_call_id", "") for m in messages if m.get("role") == "tool"]
        sample_args = [(name, list(args.keys())) for name, args in list(call_map.values())[:4]]
        logger.debug(
            "file_cache_extract_debug",
            total_messages=len(messages),
            role_counts=role_counts,
            call_map_size=len(call_map),
            tool_result_count=len(tool_msg_ids),
            sample_call_names=list({v[0] for v in call_map.values()})[:8],
            sample_tool_ids_match=[tid for tid in tool_msg_ids[:4] if tid in call_map],
            sample_args=sample_args,
        )

    # Match tool results to calls
    results = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tc_id = msg.get("tool_call_id", "")
        if tc_id not in call_map:
            continue
        name, args = call_map[tc_id]
        if _is_compressible_tool(name):
            continue  # search/grep/web — not file content
        content = _normalize_message_content(msg.get("content", ""))
        if not content.strip():
            continue
        path = _extract_tool_path(args)
        if not path:
            continue
        results.append({
            "path": path, "content": content,
            "tool_call_id": tc_id, "tool_name": name,
        })
    return results


def _extract_user_message(messages: list[dict]) -> str:
    """Extract the last user message text from a message list."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return str(content)
    return ""


def _collect_recent_tool_results(messages: list[dict], max_chars: int = 4000) -> str | None:
    """Serialize the newest tool results first within the extraction budget."""
    recent_entries: list[str] = []
    used = 0

    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue

        content = _normalize_message_content(msg.get("content")).strip()
        if not content:
            continue

        tool_name = msg.get("name", "unknown_tool")
        # Apply RTK Layer 1 filter before packing into the extraction budget —
        # strips noise/boilerplate so the extractor LLM sees signal, not lint.
        content = filter_single_tool_result(content, tool_name=tool_name)
        entry = f"Tool [{tool_name}]:\n{content}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(entry) > remaining:
            entry = entry[:remaining]
        recent_entries.append(entry)
        used += len(entry)

    if not recent_entries:
        return None

    recent_entries.reverse()
    return "\n\n".join(recent_entries)
