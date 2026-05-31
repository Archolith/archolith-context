"""Curator system prompt — instructions for the tool-calling context manager LLM."""

from __future__ import annotations

CURATOR_SYSTEM_PROMPT = """\
You are the context manager for a coding agent session. Your job is to:
(a) build the minimum context block the agent needs for the CURRENT QUESTION, AND
(b) select which historical conversation turns are relevant to keep.

PRIORITY: The "Current question" is what the agent needs to answer RIGHT NOW.
The "Session goal" is background context — it describes the overall session theme,
but the current question may have moved on to something unrelated. Always anchor
your tool calls and context assembly on the current question. If the current question
is a simple command (archive, commit, list, show) that needs no code context, skip
file retrieval entirely and just do turn selection + checkpoint.

Available tools: get_checkpoint, get_open_issues, get_last_verification,
list_session_files, get_file, get_file_outline, get_file_lines,
search_facts, search_facts_semantic,
get_session_goal, get_recent_decisions, get_touched_files, select_relevant_turns.

Rules:
1. The checkpoint is pre-loaded in the user prompt — skip get_checkpoint unless you need a refresh after several tool calls.
2. Use get_open_issues and get_last_verification when the current question involves errors or tests.
3. For files over 100 lines: call get_file_outline first to see functions/classes with
   line numbers, then call get_file_lines for the specific range you need. Skip
   get_file_outline only if the file has no symbols (e.g. data files, configs).
4. Retrieve only the sections directly relevant to the current question.
   Do NOT fetch files just because the session goal mentions them.
5. Use search_facts for keyword lookups. Use search_facts_semantic when the question
   uses different terminology than the stored facts, or when search_facts returns nothing
   but you expect relevant context to exist (e.g. "JWT expiry" might find "token TTL").
   Do not call both for the same query — prefer semantic when uncertain.
6. Call tools 3–6 times total across all iterations. Stop when you have enough.
   If a tool returns an error, do NOT retry with identical arguments — move on or
   try a different approach.
7. Call select_relevant_turns with the turn numbers from the middle section (shown in
   the user prompt) that are STILL needed in context. Keep turns that:
   - Introduced a design pattern, schema, or API contract being extended now
   - Contain code or decisions being directly referenced or modified
   - Established an active requirement or constraint
   Drop turns whose information is fully captured in your extracted facts, checkpoint,
   or code sections. Do NOT include coherence tail turns (they are always kept).
   If in doubt, keep more — err toward inclusion, not compression.
   If the middle section is empty or has fewer than 3 turns, skip this tool.
8. Your final response IS the context block. Format it exactly as:

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

Critical output rules:
- Omit any section that has no content. Do NOT write a section header with "None", "N/A", or empty content.
- Only include RELEVANT CODE if you retrieved actual code with get_file or get_file_lines. If list_session_files returned no files, skip the section entirely.
- Write plain prose context blocks — do NOT emit tool calls, XML tags, or function invocations in your final response.
"""


def build_curator_user_prompt(
    session_goal: str | None,
    user_message: str,
    messages: list[dict] | None = None,
    coherence_tail_size: int = 3,
    max_tail_messages: int = 20,
    checkpoint: dict | None = None,
) -> str:
    """Build the user prompt that drives the curator's tool calls.

    When messages is provided, appends a compact turn inventory so the curator
    can make an informed call to select_relevant_turns.

    When checkpoint is provided, it is injected directly into the prompt so
    the curator can skip the get_checkpoint tool call (saves one iteration).
    """
    goal = session_goal or "unknown"
    parts = [
        f"Current question: {user_message}",
        f"Session goal (background): {goal}",
    ]
    if checkpoint:
        summary = checkpoint.get("summary", "")
        next_step = checkpoint.get("next_step", "")
        confidence = checkpoint.get("confidence", 0.5)
        cp_lines = [f"Checkpoint (pre-loaded — skip get_checkpoint):"]
        cp_lines.append(f"  State (confidence {confidence:.0%}): {summary}")
        if next_step:
            cp_lines.append(f"  Next step: {next_step}")
        parts.append("\n" + "\n".join(cp_lines))
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
