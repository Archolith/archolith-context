"""Curator system prompt — instructions for the tool-calling context manager LLM."""

from __future__ import annotations

CURATOR_SYSTEM_PROMPT = """\
You are the context manager for a coding agent session. Your job is to:
(a) build the minimum context block the agent needs for the current task step, AND
(b) select which historical conversation turns are relevant to keep.

Available tools: get_checkpoint, get_open_issues, get_last_verification,
list_session_files, get_file, get_file_lines, search_facts,
get_session_goal, get_recent_decisions, get_touched_files, select_relevant_turns.

Rules:
1. Start with get_checkpoint — it tells you where the session stands in one call.
2. Use get_open_issues and get_last_verification when the question involves errors or tests.
3. Use get_file_lines, not get_file, for any file over 50 lines.
4. Retrieve only the sections directly relevant to the current question.
5. Call tools 3–6 times total across all iterations. Stop when you have enough.
6. Call select_relevant_turns with the turn numbers from the middle section (shown in
   the user prompt) that are STILL needed in context. Keep turns that:
   - Introduced a design pattern, schema, or API contract being extended now
   - Contain code or decisions being directly referenced or modified
   - Established an active requirement or constraint
   Drop turns whose information is fully captured in your extracted facts, checkpoint,
   or code sections. Do NOT include coherence tail turns (they are always kept).
   If in doubt, keep more — err toward inclusion, not compression.
   If the middle section is empty or has fewer than 3 turns, skip this tool.
7. Your final response IS the context block. Format it exactly as:

=== SESSION GOAL ===
<goal>

=== CURRENT STATE ===
<checkpoint summary and next step>

=== OPEN ISSUES ===
- <issue>

=== LAST VERIFICATION ===
<command and result>

=== RELEVANT CODE ===
<path> lines <start>-<end>:
```
<code>
```

=== KEY FACTS ===
- <fact>

=== DECISIONS ===
- <decision>

If a section has no content, omit it entirely.
"""


def build_curator_user_prompt(
    session_goal: str | None,
    user_message: str,
    messages: list[dict] | None = None,
    coherence_tail_size: int = 3,
    max_tail_messages: int = 20,
) -> str:
    """Build the user prompt that drives the curator's tool calls.

    When messages is provided, appends a compact turn inventory so the curator
    can make an informed call to select_relevant_turns.
    """
    goal = session_goal or "unknown"
    parts = [
        f"Session goal: {goal}",
        f"Current question: {user_message}",
    ]
    if messages:
        inventory = _build_turn_inventory(messages, coherence_tail_size, max_tail_messages)
        if inventory:
            parts.append("\n" + inventory)
    return "\n".join(parts)


def _build_turn_inventory(
    messages: list[dict],
    coherence_tail_size: int,
    max_tail_messages: int,
) -> str:
    """Build a compact per-turn summary for the curator's select_relevant_turns decision.

    Returns an empty string when there are no middle turns to select from
    (all messages are in the cold-start window or coherence tail).
    """
    try:
        from archolith_proxy.assembler.tail import smart_tail
    except ImportError:
        return ""

    non_system = [m for m in messages if m.get("role") != "system"]
    if not non_system:
        return ""

    tail = smart_tail(non_system, base_size=coherence_tail_size, max_size=max_tail_messages)
    tail_start = len(non_system) - len(tail)
    middle = non_system[:tail_start]

    if not middle:
        return ""  # All messages are in the tail — nothing to select from

    # Assign absolute turn numbers across the full non-system message list.
    # Turn N starts at the Nth user message; all following messages until the
    # next user message share that turn number.
    turn_num = 0
    all_turn_nums: list[int] = []
    for msg in non_system:
        if msg.get("role") == "user":
            turn_num += 1
        all_turn_nums.append(turn_num)

    middle_turn_nums = all_turn_nums[:tail_start]
    tail_turn_nums = all_turn_nums[tail_start:]

    # Build per-turn summaries for middle messages only.
    # Each turn: first user message preview + first assistant response preview.
    turns_seen: dict[int, dict[str, str]] = {}  # turn_num -> {user, asst}
    for i, msg in enumerate(middle):
        t = middle_turn_nums[i]
        if t not in turns_seen:
            turns_seen[t] = {"user": "", "asst": ""}
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and "text" in p
            )
        content = str(content).replace("\n", " ")
        if role == "user" and not turns_seen[t]["user"]:
            turns_seen[t]["user"] = content[:80]
        elif role == "assistant" and not turns_seen[t]["asst"]:
            turns_seen[t]["asst"] = content[:60]

    lines = ["Conversation turns (middle section — call select_relevant_turns to choose which to keep):"]
    for t_num in sorted(turns_seen.keys()):
        td = turns_seen[t_num]
        line = f'  [t{t_num}] "{td["user"]}"'
        if td["asst"]:
            line += f' -> "{td["asst"]}"'
        lines.append(line)

    # Show tail range so the curator knows not to include those
    if tail_turn_nums:
        min_t = tail_turn_nums[0]
        max_t = max(tail_turn_nums)
        if min_t == max_t:
            lines.append(
                f"\nCoherence tail (always kept — do NOT include in select_relevant_turns): t{min_t}"
            )
        else:
            lines.append(
                f"\nCoherence tail (always kept — do NOT include in select_relevant_turns): t{min_t}–t{max_t}"
            )

    lines.append(
        "\nCall select_relevant_turns with the t-numbers from the middle section you want to retain."
    )

    return "\n".join(lines)
