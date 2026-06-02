"""SessionBriefing — pre-built context snapshot from the background curator pass.

The background pass runs the same curator bot with a generous iteration budget
(12 iterations) and captures its output into a SessionBriefing. The next inline
pass reads the briefing and can produce a context block in ~1 iteration instead
of 4-6, cutting latency from 3-6s to <1.5s.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PreFetchedFile:
    """A file section the background pass retrieved."""

    path: str
    outline: str  # structural outline (from get_file_outline)
    sections: list[tuple[int, int, str]]  # (start, end, content) fetched ranges
    relevance: str  # why the curator fetched this


@dataclass
class SessionBriefing:
    """Output of the background curator pass — everything the inline pass needs.

    Intentionally a data-only snapshot. The background pass populates it by
    running the normal curator loop. The inline pass receives it as formatted
    text injected into the user prompt.
    """

    session_id: str
    source_turn: int  # turn this was built after
    timestamp: float = field(default_factory=time.time)

    # Session state (pre-fetched by background pass)
    checkpoint_text: str = ""
    open_issues_text: str = ""
    last_verification_text: str = ""
    decisions_text: str = ""
    session_goal: str = ""
    facts_text: str = ""

    # Pre-fetched files (the expensive part)
    files: list[PreFetchedFile] = field(default_factory=list)

    # Turn selection (pre-computed)
    retained_turns: list[int] | None = None
    turn_inventory: str = ""

    # The fully assembled context block from the background pass
    context_block: str = ""

    # Curation mode that produced this briefing
    # "two_pass" = single-bot two-pass (default), "two_curator" = prepper/assembler
    mode: str = "two_pass"

    # Metadata
    tool_calls_used: int = 0
    iterations_used: int = 0
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# Cap briefing text at this many chars to avoid bloating the inline prompt
_BRIEFING_MAX_CHARS = 30_000


def format_briefing_for_prompt(briefing: SessionBriefing) -> str:
    """Format a SessionBriefing as a prompt section for the inline curator.

    The inline curator sees its own prior output pre-loaded and just needs to:
    1. Check if the current question changes anything
    2. Possibly call select_relevant_turns if the turn inventory changed
    3. Emit the final context block
    """
    parts = [
        "Previous curator context (from background pass — pre-fetched with full tool access):\n",
    ]

    if briefing.session_goal:
        parts.append(f"=== SESSION GOAL ===\n{briefing.session_goal}\n")

    if briefing.checkpoint_text:
        parts.append(f"=== CURRENT STATE ===\n{briefing.checkpoint_text}\n")

    if briefing.open_issues_text:
        parts.append(f"=== OPEN ISSUES ===\n{briefing.open_issues_text}\n")

    if briefing.last_verification_text:
        parts.append(f"=== LAST VERIFICATION ===\n{briefing.last_verification_text}\n")

    # Pre-fetched file sections
    if briefing.files:
        file_parts = []
        for f in briefing.files:
            if f.sections:
                for start, end, content in f.sections:
                    file_parts.append(f"{f.path} lines {start}-{end}:\n```\n{content}\n```\n")
            elif f.outline:
                file_parts.append(f"{f.path} outline:\n{f.outline}\n")
        if file_parts:
            parts.append("=== RELEVANT CODE ===\n" + "\n".join(file_parts))

    if briefing.facts_text:
        parts.append(f"=== KEY FACTS ===\n{briefing.facts_text}\n")

    if briefing.decisions_text:
        parts.append(f"=== DECISIONS ===\n{briefing.decisions_text}\n")

    if briefing.retained_turns is not None:
        parts.append(f"=== RETAINED TURNS ===\n{briefing.retained_turns}\n")

    # Cap total size
    combined = "\n".join(parts)
    if len(combined) > _BRIEFING_MAX_CHARS:
        combined = combined[:_BRIEFING_MAX_CHARS] + "\n... [briefing truncated at 30K chars]"

    combined += (
        f"\nThis context was built after turn {briefing.source_turn}. "
        "The current question may need adjustments. "
        "If the pre-fetched context fully covers the current question, emit it directly "
        "(adjust wording if needed). If the current question needs additional files or "
        "different sections, call tools to fetch them — but most work is already done."
    )

    return combined
