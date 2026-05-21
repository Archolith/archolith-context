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
    """Rewrite the messages array: merge graph context into system prompt + coherence tail.

    Strategy:
    1. Merge graph-assembled context INTO the original system message
       (NVIDIA API rejects multiple consecutive system messages)
    2. Keep the last N messages as the "coherence tail" (recent context the model needs)
    3. Discard the middle messages (replaced by graph context)

    This reduces a 100K+ token linear history to ~15-20K of curated context.
    """
    if not assembled_context or not assembled_context.graph_context:
        return original_messages

    result = []

    # 1. Merge graph context into the original system message
    system_msg = None
    rest = []
    for msg in original_messages:
        if msg.get("role") == "system" and system_msg is None:
            system_msg = msg.copy()
        else:
            rest.append(msg)

    # Build the combined system message: original + graph context
    graph_content = "\n\n".join(
        m.get("content", "") for m in assembled_context.graph_context
    )
    if system_msg:
        system_msg["content"] = system_msg.get("content", "") + "\n\n" + graph_content
        result.append(system_msg)
    else:
        # No original system message — graph context becomes the system message
        result.append({"role": "system", "content": graph_content})

    # 2. Keep the coherence tail — use smart_tail to preserve tool-call integrity
    from src.assembler.tail import smart_tail

    tail = smart_tail(rest, base_size=coherence_tail_size, max_size=max_tail_messages)

    # 3. Ensure role alternation: after system messages, the first non-system
    # message must be 'user'. Strip any leading assistant/tool messages.
    while tail and tail[0].get("role") not in ("user",):
        tail = tail[1:]

    # 4. Validate alternation: merge any consecutive duplicate roles
    validated_tail = []
    for msg in tail:
        if validated_tail:
            prev_role = validated_tail[-1].get("role")
            curr_role = msg.get("role")
            if prev_role == "user" and curr_role == "user":
                prev_content = validated_tail[-1].get("content", "")
                curr_content = msg.get("content", "")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    validated_tail[-1]["content"] = prev_content + "\n\n" + curr_content
                    continue
            if prev_role == "assistant" and curr_role == "assistant":
                prev_content = validated_tail[-1].get("content", "")
                curr_content = msg.get("content", "")
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    validated_tail[-1]["content"] = prev_content + "\n\n" + curr_content
                    continue
        validated_tail.append(msg)

    result.extend(validated_tail)

    return result
