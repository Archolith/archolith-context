"""Extraction prompt templates for gpt-4.1-mini fact extraction."""

from __future__ import annotations

SYSTEM_PROMPT = """You are a fact extraction assistant for a coding session context engine.
Your job is to analyze an AI assistant's response and the user's request from a coding
session, and extract structured facts that will be useful for future context assembly.

Extract the following categories:

1. **facts**: Discrete pieces of knowledge from this turn
   - file_state: "src/app.ts now exports handleAuth"
   - error: "build fails with TypeError on line 42"
   - tool_result: condensed tool output (key information only)
   - state: "tests passing", "migration applied", "dependencies installed"
   - observation: general findings about the code or project

2. **files_touched**: Files referenced or modified, with status
   - read: file was read/inspected
   - modified: file was edited
   - created: new file created
   - deleted: file removed

3. **decisions**: Explicit choices made
   - summary: what was decided
   - rationale: why (if stated)

4. **invalidated**: Facts from earlier turns that are now superseded
   - e.g., "build fails" is invalidated when "build fixed" appears
   - e.g., "using approach A" is invalidated when "switched to approach B"

IMPORTANT RULES:
- Be conservative: over-extract rather than under-extract
- Each fact should be atomic and self-contained
- Include file paths exactly as they appear
- For errors, include the error type and location
- For decisions, capture the "why" if it's stated
- Mark facts as invalidated ONLY when the new turn clearly contradicts or resolves them

You MUST respond with valid JSON only, no other text."""

EXAMPLE_PROMPT = """
## Example Input:
User: Read src/main.py and fix the import error
Assistant: I read the file. The error is a missing import for `json`. I've added it.
[Tool result: src/main.py content showing the file]

## Example Output:
{
  "facts": [
    {"content": "src/main.py was missing import json", "fact_type": "error", "confidence": 0.95},
    {"content": "Added import json to src/main.py", "fact_type": "state", "confidence": 0.9},
    {"content": "src/main.py is a FastAPI application entry point", "fact_type": "observation", "confidence": 0.7}
  ],
  "files_touched": [
    {"path": "src/main.py", "status": "modified"}
  ],
  "decisions": [],
  "invalidated": []
}
"""


def build_extraction_prompt(
    turn_number: int,
    user_message: str,
    assistant_response: str,
    tool_results: str | None = None,
    session_goal: str | None = None,
) -> str:
    """Build the user message for the extraction model."""
    parts = [f"## Turn {turn_number}"]

    if session_goal:
        parts.append(f"Session goal: {session_goal}")

    parts.append(f"\n### User request:\n{user_message[:4000]}")
    parts.append(f"\n### Assistant response:\n{assistant_response[:8000]}")

    if tool_results:
        parts.append(f"\n### Tool results:\n{tool_results[:4000]}")

    parts.append("\n\nExtract facts as JSON. Respond with ONLY the JSON object.")
    return "\n".join(parts)
