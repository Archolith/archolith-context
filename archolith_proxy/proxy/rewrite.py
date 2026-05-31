"""Message rewriting and token estimation — extracted from openai/chat.py.

Pure functions with no graph coupling. Handles:
- Stripping model reasoning/thinking blocks
- Estimating input token counts
- Rewriting messages arrays to merge graph context + coherence tail
"""

from __future__ import annotations

import re

from archolith_proxy.rtk import shrink_tail_tool_results

# Pattern for stripping model reasoning/thinking blocks before extraction
_REASONING_PATTERN = re.compile(
    r"<(?:thinking|reasoning|inner_monologue)>.*?</(?:thinking|reasoning|inner_monologue)>",
    re.DOTALL,
)

# DSML tool-call artifacts emitted by DeepSeek when it attempts to invoke tools
# from within its response text. These appear when the model sees DSML-format
# tool outputs in its history (from a prior cold-start turn where the user
# mentioned a file path) and continues the pattern.
#
# Removing these from retained assistant messages prevents the pattern from
# propagating into subsequent turns via the curator's retained history.
#
# Patterns handled:
# - DeepSeek DSML: <｜｜DSML｜｜...> (fullwidth pipes, U+FF5C)
# - DeepSeek V3 tool-call block: <｜tool▁calls▁begin｜> ... <｜tool▁calls▁end｜>
# - Nous-style tool calls: <tool_call>...</tool_call>
_DSML_BLOCK_RE = re.compile(
    r"<｜｜DSML｜｜.*",  # strip from first DSML marker to end
    re.DOTALL,
)
_NOUS_TOOL_CALL_RE = re.compile(
    r"<tool_call>.*?</tool_call>",
    re.DOTALL,
)
_DEEPSEEK_TOOL_BLOCK_RE = re.compile(
    r"<｜tool▁calls▁begin｜>.*?<｜tool▁calls▁end｜>",
    re.DOTALL,
)


def strip_reasoning(text: str) -> str:
    """Strip model reasoning blocks before extraction.

    Models that emit <thinking>/<reasoning> blocks include internal scaffolding
    (tentative reasoning, abandoned approaches, self-corrections) that isn't useful
    as facts. Stripping prevents noise in the extraction pipeline.
    """
    return _REASONING_PATTERN.sub("", text).strip()


def strip_dsml_artifacts(text: str) -> str:
    """Strip DSML tool-call artifacts from assistant message text.

    DeepSeek emits DSML-format tool invocations (``<｜｜DSML｜｜...>``) when it
    thinks it is in a tool-enabled session. When a prior turn containing this
    markup is retained in the curator's rewritten history, the model sees its
    own DSML output and repeats the pattern on the next turn.

    This function removes the artifact markup from assistant messages before
    they are passed upstream, breaking the contamination cycle. The prose
    content before the first DSML tag is preserved.
    """
    if not text:
        return text
    # DeepSeek DSML: everything from the first marker to end-of-string
    text = _DSML_BLOCK_RE.sub("", text)
    # DeepSeek V3 tool block delimiters (bounded — remove the whole block)
    text = _DEEPSEEK_TOOL_BLOCK_RE.sub("", text)
    # Nous-style XML tool calls (bounded)
    text = _NOUS_TOOL_CALL_RE.sub("", text)
    return text.rstrip()


def estimate_input_tokens(messages: list[dict]) -> int:
    """Estimate total input tokens using tiktoken cl100k_base with 10% margin + 500 floor."""
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    total_tokens = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multi-part content
            for part in content:
                if isinstance(part, dict):
                    total_tokens += len(enc.encode(part.get("text", "")))
        elif isinstance(content, str):
            total_tokens += len(enc.encode(content))
    with_margin = int(total_tokens * 1.10)
    return max(with_margin, 500)


def rewrite_messages(
    original_messages: list[dict],
    assembled_context,
    coherence_tail_size: int,
    max_tail_messages: int = 20,
    max_rewritten_tokens: int = 0,
) -> list[dict]:
    """Rewrite the messages array: curator turn selection + tool result compression.

    Strategy:
    1. Keep the original system message + injected graph context (goal, facts, files)
    2. For the middle (non-tail) portion:
       - If the curator provided retained_turn_numbers, drop turns not in that set.
         A "turn" is the user message and all messages that follow until the next
         user message (preserves tool-call structural integrity within a turn).
       - Tool results (file reads, search outputs): compress to a short preview.
         These are the expensive blobs; extracted facts in the system message carry
         the semantic content the model needs without re-reading.
       - Assistant messages: keep intact. Compressing them removes intermediate
         analysis and causes the model to re-read files to reconstruct its reasoning.
       - User messages: keep intact verbatim.
    3. Keep the coherence tail (last N exchanges) completely intact.

    Token savings come from (a) dropping irrelevant historical turns selected by
    the curator, and (b) compressing tool result blobs (5K-50K chars each).
    """
    if not assembled_context or not getattr(assembled_context, "graph_context", None):
        return original_messages

    # Curator turn selection — None means keep all (backward compatible)
    retained_turn_numbers = getattr(assembled_context, "retained_turn_numbers", None)
    retained_set = set(retained_turn_numbers) if retained_turn_numbers is not None else None

    # Split: system message, non-system messages
    system_msg = None
    rest = []
    for msg in original_messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = msg.copy()
        else:
            rest.append(msg)

    # Identify the coherence tail
    from archolith_proxy.assembler.tail import smart_tail
    tail = smart_tail(rest, base_size=coherence_tail_size, max_size=max_tail_messages)
    tail_start = len(rest) - len(tail) if tail else len(rest)
    middle = rest[:tail_start]

    # Assign ABSOLUTE turn numbers to middle messages.
    # Turn numbers match the curator's turn inventory (1-indexed across the full
    # conversation, not just the middle). The curator receives these absolute
    # numbers in the prompt and calls select_relevant_turns([3, 5, 8, ...]).
    if retained_set is not None:
        turn_num = 0
        all_rest_turn_nums: list[int] = []
        for msg in rest:  # count across the full non-system portion
            if msg.get("role") == "user":
                turn_num += 1
            all_rest_turn_nums.append(turn_num)
        middle_turn_nums: list[int] = all_rest_turn_nums[:tail_start]
    else:
        middle_turn_nums = []  # unused when retained_set is None

    # Build the session overview from graph context (goal, facts, decisions)
    system_context = getattr(assembled_context, "system_message", None) or {}
    graph_context = getattr(assembled_context, "graph_context", None) or []
    graph_content = system_context.get("content", "") if isinstance(system_context, dict) else ""
    if not graph_content:
        graph_content = "\n\n".join(
            m.get("content", "") for m in graph_context if isinstance(m, dict)
        )

    # Build result: system message with session overview
    result = []
    if system_msg:
        system_msg["content"] = system_msg.get("content", "") + "\n\n" + graph_content
        result.append(system_msg)
    else:
        result.append({"role": "system", "content": graph_content})

    # Rewrite the middle.
    for i, msg in enumerate(middle):
        # Turn selection: skip messages from turns the curator did not retain
        if retained_set is not None and middle_turn_nums[i] not in retained_set:
            continue

        role = msg.get("role")
        if role == "tool":
            # Tool results are often the primary token cost, but not all tool results
            # are safe to compress. File reads that the model may need to edit require
            # exact line content. Only compress results from tools that return
            # informational/search output, not file content the model may reference.
            #
            # Compressible: search, grep, list_directory, web_fetch, find — large
            #   outputs where facts have been extracted to graph context.
            # Preserved: read, glob, cat, head, tail, and unknown tools — the model
            #   may need exact line numbers or content for edits.
            tool_name = (msg.get("name") or "").lower()
            content = msg.get("content", "") or ""
            if _is_compressible_tool(tool_name) and len(content) > _TOOL_RESULT_MAX_CHARS:
                if isinstance(content, str):
                    result.append({**msg, "content": _compress_tool_result(content)})
                elif isinstance(content, list):
                    result.append({**msg, "content": _compress_tool_result_multipart(content)})
                else:
                    result.append(msg)
            else:
                result.append(msg)
        elif role == "assistant":
            # Keep assistant messages intact — they're the model's reasoning chain.
            # Strip two categories of scaffolding that contaminate downstream turns:
            # 1. <thinking>/<reasoning> blocks — internal monologue, not needed.
            # 2. DSML tool-call artifacts — DeepSeek emits these when it thinks it's
            #    in a tool-enabled session. Retaining them causes the pattern to
            #    propagate: the model sees its own prior DSML and repeats it.
            content = msg.get("content", "") or ""
            if isinstance(content, str) and content:
                cleaned = strip_reasoning(content)
                cleaned = strip_dsml_artifacts(cleaned)
                if cleaned != content:
                    result.append({**msg, "content": cleaned})
                else:
                    result.append(msg)
            else:
                result.append(msg)
        else:
            # user messages and any other roles — keep intact
            result.append(msg)

    # Append the coherence tail — shrink oversized tool results first so large
    # file reads / command outputs don't dominate the context window even when
    # kept for structural integrity.  Fail-open: if RTK absent, tail is intact.
    tail_validated = _validate_tail(tail)
    tail_shrunk = shrink_tail_tool_results(tail_validated)
    result.extend(tail_shrunk)

    # Token ceiling: if the rewritten payload exceeds the cap, progressively
    # compress tool results in the middle section (oldest retained turns first).
    # This prevents the rewritten output from growing indefinitely as the
    # curator accumulates more retained turns.
    if max_rewritten_tokens > 0:
        result = _enforce_token_ceiling(result, max_rewritten_tokens, len(result) - len(tail_shrunk))

    # Final validation: ensure first non-system is user
    first_non_system = next((i for i, m in enumerate(result) if m.get("role") != "system"), None)
    if first_non_system is not None and result[first_non_system].get("role") != "user":
        result = [m for m in result[:first_non_system] if m.get("role") == "system"] + \
                 [m for m in result[first_non_system:] if m.get("role") == "user" or
                  result.index(m) > first_non_system or m.get("role") == "system"]
        result = _ensure_user_first(result)

    return result


# Max chars to keep from a compressible tool result in the middle section.
_TOOL_RESULT_MAX_CHARS = 400

# Tools whose results are safe to compress in the middle section.
# These return informational/search output, not file content the model needs
# for edits or exact line references.
_COMPRESSIBLE_TOOLS = frozenset({
    # Search and grep
    "search", "grep", "ripgrep", "find", "findfiles",
    "web_search", "websearch",
    # Directory and file listing (not file content)
    "list_directory", "listdir", "ls", "glob",
    # Web content (informational)
    "web_fetch", "webfetch", "fetch",
    # Shell commands that return informational output
    "bash", "shell", "run_command", "execute",
})


def _is_compressible_tool(tool_name: str) -> bool:
    """Return True if this tool's result is safe to compress in the middle section.

    File-read tools (read, cat, head, tail, etc.) return exact content the model
    may need for edits or line references — they must be preserved verbatim.
    Search/list/web tools return informational output that's captured in the
    extracted facts; their large results can be compressed.
    """
    if not tool_name:
        return False  # Unknown tool — preserve to be safe
    # Exact match against the compressible set
    if tool_name in _COMPRESSIBLE_TOOLS:
        return True
    # Prefix match for namespaced tools (e.g. "mcp__brave__search")
    for compressible in _COMPRESSIBLE_TOOLS:
        if tool_name.endswith(f"__{compressible}") or tool_name.endswith(f"_{compressible}"):
            return True
    return False


def _compress_tool_result(content: str) -> str:
    """Compress a tool result (file read, search output) to a short preview.

    Tool results in the middle section are the primary token cost — raw file
    contents can be 5K-50K chars each. Keeping a short preview lets the model
    identify what was read; extracted facts in the injected system context carry
    the semantic content needed to avoid re-reading.

    Breaks at the last newline within the preview budget to avoid mid-line cuts.
    """
    if len(content) <= _TOOL_RESULT_MAX_CHARS:
        return content
    preview = content[:_TOOL_RESULT_MAX_CHARS]
    nl = preview.rfind("\n")
    if nl > _TOOL_RESULT_MAX_CHARS // 2:
        preview = preview[:nl]
    omitted = len(content) - len(preview)
    return f"{preview}\n[...{omitted} chars — key facts in session context above...]"


def _compress_tool_result_multipart(parts: list) -> list:
    """Compress multi-part tool result content (list-of-dicts format)."""
    result = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text", "")
            result.append({**part, "text": _compress_tool_result(text)})
        else:
            result.append(part)
    return result


def _compress_assistant_message(content: str, max_chars: int = 300) -> str:
    """Compress a long assistant response to its key content.

    No longer used in the middle section rewriter (assistant messages are kept
    intact to preserve reasoning chains). Retained for other callers.

    Keeps the first and last portions, dropping the verbose middle.
    """
    if len(content) <= max_chars:
        return content
    head_budget = max_chars * 2 // 3
    tail_budget = max_chars // 3
    head = content[:head_budget].rsplit(" ", 1)[0]
    tail = content[-tail_budget:].split(" ", 1)[-1] if tail_budget > 0 else ""
    omitted = len(content) - len(head) - len(tail)
    return f"{head}\n[...{omitted} chars compressed...]\n{tail}"


def _enforce_token_ceiling(
    messages: list[dict],
    max_tokens: int,
    tail_start_idx: int,
) -> list[dict]:
    """Progressively compress middle-section tool results until under the token ceiling.

    When the rewritten payload exceeds max_tokens, compress tool results in the
    middle section (between system msg and tail) from oldest to newest. Each
    oversized tool result gets truncated to _CEILING_COMPRESS_CHARS. If still
    over budget after compressing all tool results, compress assistant messages
    in the middle from oldest to newest.

    The tail is never modified — it needs exact content for the current turn.
    """
    current = estimate_input_tokens(messages)
    if current <= max_tokens:
        return messages

    import structlog
    logger = structlog.get_logger()

    result = list(messages)

    # Phase 1: compress tool results in the middle (oldest first)
    for i in range(tail_start_idx):
        if estimate_input_tokens(result) <= max_tokens:
            break
        msg = result[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "") or ""
        if isinstance(content, str) and len(content) > _CEILING_COMPRESS_CHARS:
            result[i] = {**msg, "content": _compress_tool_result_ceiling(content)}

    # Phase 2: if still over, compress assistant messages in the middle (oldest first)
    if estimate_input_tokens(result) > max_tokens:
        for i in range(tail_start_idx):
            if estimate_input_tokens(result) <= max_tokens:
                break
            msg = result[i]
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "") or ""
            if isinstance(content, str) and len(content) > _CEILING_COMPRESS_CHARS:
                result[i] = {**msg, "content": _compress_assistant_message(content, _CEILING_COMPRESS_CHARS)}

    final_tokens = estimate_input_tokens(result)
    logger.info(
        "token_ceiling_enforced",
        before=current,
        after=final_tokens,
        ceiling=max_tokens,
        compressed_to_budget=final_tokens <= max_tokens,
    )
    return result


# Max chars for tool results when the token ceiling forces compression.
# Larger than _TOOL_RESULT_MAX_CHARS (400) since these are file reads
# the model may still reference — keep enough for identification.
_CEILING_COMPRESS_CHARS = 800


def _compress_tool_result_ceiling(content: str) -> str:
    """Compress a tool result under the token ceiling — keeps more than middle compression."""
    if len(content) <= _CEILING_COMPRESS_CHARS:
        return content
    preview = content[:_CEILING_COMPRESS_CHARS]
    nl = preview.rfind("\n")
    if nl > _CEILING_COMPRESS_CHARS // 2:
        preview = preview[:nl]
    omitted = len(content) - len(preview)
    return f"{preview}\n[...{omitted} chars truncated by context ceiling...]"


def _validate_tail(tail: list[dict]) -> list[dict]:
    """Ensure the tail starts with a user message and has valid role alternation."""
    # Strip leading non-user messages
    while tail and tail[0].get("role") not in ("user",):
        tail = tail[1:]

    # Merge consecutive same-role messages
    validated = []
    for msg in tail:
        if validated:
            prev_role = validated[-1].get("role")
            curr_role = msg.get("role")
            if prev_role == curr_role and curr_role in ("user", "assistant"):
                prev_content = validated[-1].get("content", "")
                curr_content = msg.get("content", "")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    validated[-1] = {**validated[-1], "content": prev_content + "\n\n" + curr_content}
                    continue
        validated.append(msg)
    return validated


_DEEPSEEK_NO_TOOL_HINT = (
    "Respond with plain text, markdown, and code blocks only. "
    "Do not emit tool calls, function invocations, DSML markup, "
    "or any other special invocation syntax in your response."
)


def inject_no_dsml_hint(messages: list[dict], model: str, has_tools: bool = False) -> list[dict]:
    """Inject a plain-text instruction into the system prompt for DeepSeek models.

    DeepSeek emits DSML tool-call markup when it infers it is in a tool-enabled
    session (e.g., user mentions file paths). This instruction explicitly tells
    it to respond in plain text only.

    Only injected when:
    - The model name contains "deepseek" (case-insensitive).
    - The request has no tools (if real tools are present, tool-calling is intentional
      and we must not suppress it).
    """
    if has_tools:
        return messages
    if "deepseek" not in model.lower():
        return messages

    result = []
    injected = False
    for msg in messages:
        if msg.get("role") == "system" and not injected:
            existing = msg.get("content", "") or ""
            result.append({**msg, "content": existing + "\n\n" + _DEEPSEEK_NO_TOOL_HINT})
            injected = True
        else:
            result.append(msg)

    if not injected:
        # No system message found — prepend one
        result.insert(0, {"role": "system", "content": _DEEPSEEK_NO_TOOL_HINT})

    return result


def _ensure_user_first(messages: list[dict]) -> list[dict]:
    """Ensure the first non-system message is a user message."""
    system_msgs = []
    rest = []
    for m in messages:
        if m.get("role") == "system" and not rest:
            system_msgs.append(m)
        else:
            rest.append(m)
    while rest and rest[0].get("role") != "user":
        rest = rest[1:]
    return system_msgs + rest
