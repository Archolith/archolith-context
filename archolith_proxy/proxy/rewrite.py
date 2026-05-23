"""Message rewriting and token estimation — extracted from openai/chat.py.

Pure functions with no graph coupling. Handles:
- Stripping model reasoning/thinking blocks
- Estimating input token counts
- Rewriting messages arrays to merge graph context + coherence tail
"""

from __future__ import annotations

import re

# Pattern for stripping model reasoning/thinking blocks before extraction
_REASONING_PATTERN = re.compile(
    r"<(?:thinking|reasoning|inner_monologue)>.*?</(?:thinking|reasoning|inner_monologue)>",
    re.DOTALL,
)


def strip_reasoning(text: str) -> str:
    """Strip model reasoning blocks before extraction.

    Models that emit <thinking>/<reasoning> blocks include internal scaffolding
    (tentative reasoning, abandoned approaches, self-corrections) that isn't useful
    as facts. Stripping prevents noise in the extraction pipeline.
    """
    return _REASONING_PATTERN.sub("", text).strip()


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
) -> list[dict]:
    """Rewrite the messages array: preserve user messages, compress assistant responses.

    Strategy:
    1. Keep the original system message + session overview (goal, files, decisions)
    2. For the middle (non-tail) portion: keep all user messages verbatim,
       replace assistant responses with per-turn fact summaries from the graph
    3. Keep the coherence tail (last N exchanges) completely intact

    This preserves the conversational flow the model needs while compressing
    the expensive assistant responses in the middle of long conversations.
    """
    if not assembled_context or not getattr(assembled_context, "graph_context", None):
        return original_messages

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

    # Build the session overview from graph context (goal, files, decisions)
    # but NOT the flat fact list — facts get woven into per-turn summaries
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

    # Rewrite the middle: keep structure intact, only compress long assistant text.
    # Tool results and tool_call chains are preserved — agentic sessions need
    # file contents and tool outputs to avoid re-reading loops.
    for msg in middle:
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content", "") or ""
            has_tool_calls = bool(msg.get("tool_calls"))
            if has_tool_calls:
                # Keep tool_calls intact so tool results stay paired
                if isinstance(content, str) and len(content) > 200:
                    result.append({**msg, "content": _compress_assistant_message(content)})
                else:
                    result.append(msg)
            elif isinstance(content, str) and len(content) > 200:
                result.append({
                    "role": "assistant",
                    "content": _compress_assistant_message(content),
                })
            else:
                result.append(msg)
        else:
            result.append(msg)

    # Append the coherence tail intact
    tail_validated = _validate_tail(tail)
    result.extend(tail_validated)

    # Final validation: ensure first non-system is user
    first_non_system = next((i for i, m in enumerate(result) if m.get("role") != "system"), None)
    if first_non_system is not None and result[first_non_system].get("role") != "user":
        result = [m for m in result[:first_non_system] if m.get("role") == "system"] + \
                 [m for m in result[first_non_system:] if m.get("role") == "user" or
                  result.index(m) > first_non_system or m.get("role") == "system"]
        result = _ensure_user_first(result)

    return result


def _compress_assistant_message(content: str, max_chars: int = 300) -> str:
    """Compress a long assistant response to its key content.

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
