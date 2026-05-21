"""Extraction prompt templates for gpt-4.1-mini fact extraction."""

from __future__ import annotations

SYSTEM_PROMPT = """You are a fact extraction assistant for a coding session context engine.
Your job is to analyze an AI assistant's response and the user's request from a coding
session, and extract structured facts that will be useful for future context assembly.

## Output Schema

You MUST respond with a single JSON object matching this exact schema:

```json
{
 "facts": [
  {"content": "<atomic fact as a single sentence>", "fact_type": "<type>", "confidence": <0.0-1.0>}
 ],
 "files_touched": [
  {"path": "<file path>", "status": "read|modified|created|deleted"}
 ],
 "decisions": [
  {"summary": "<what was decided>", "rationale": "<why, if stated, or null>"}
 ],
 "invalidated": [
  "<description of a previously-extracted fact that is now superseded>"
 ],
 "session_goal": "<one-sentence description of what the user is trying to accomplish, or null>"
}
```

## Fact Types

- **tool_result**: Key OUTPUT from tool calls — file lists, command output, error messages, search results
- **file_state**: A file's current state or what changed ("src/app.ts now exports handleAuth")
- **error**: An error encountered ("build fails with TypeError on line 42")
- **state**: A project state change ("tests passing", "migration applied", "dependencies installed")
- **decision**: A deliberate choice made ("Switched from REST to GraphQL for the API layer")
- **observation**: General findings about the code or project

## Critical Rules

1. EXTRACT RESULTS, NOT INTENT. This is the most important rule.
   - BAD: "User wants to explore yawn.frontend"
   - BAD: "Assistant plans to check the database schema"
   - BAD: "User intends to fix the login bug"
   - GOOD: "Glob found 14 .tsx files in src/components/"
   - GOOD: "psql schema query returned: users, sessions, tokens tables"
   - GOOD: "npm test output: 42 passed, 3 failed (auth.test.ts)"
   Intent is useless for future context. Concrete results are essential.

2. EXTRACT TOOL OUTPUT. When the assistant runs tools (Glob, Grep, Read, bash, etc.),
   extract the key findings from the tool result, not the fact that a tool was called.
   - BAD: "Assistant searched for TypeScript files"
   - GOOD: "12 TypeScript files found in src/, largest is api.ts (340 lines)"
   - BAD: "Assistant ran the test suite"
   - GOOD: "pytest: 48 passed, 2 failed in test_auth.py (import error, missing fixture)"

3. Each fact MUST be atomic and self-contained — a single verifiable statement.
   - BAD: "test_calculator.py contains tests for add and subtract methods"
   - GOOD: "test_calculator.py contains test_add function"
   - GOOD: "test_calculator.py contains test_subtract function"

4. Include concrete values: file paths, function names, line numbers, error text,
   command output, counts. Vague facts are nearly worthless.
   - BAD: "There was an error in the build"
   - GOOD: "tsc build error: Type 'string' is not assignable to type 'number' at src/api.ts:42"

5. Include file paths exactly as they appear in the response.

6. For errors, include the error type, message, and location.

7. For decisions, capture the "why" if it is stated.

8. Mark facts as invalidated ONLY when the new turn clearly contradicts or resolves them.

9. Set confidence based on how certain the fact is:
   - 0.9-1.0: Directly stated or shown in code/output
   - 0.7-0.89: Strongly implied
   - 0.5-0.69: Inferred

10. Be conservative: over-extract rather than under-extract, but keep each fact atomic.

11. ALWAYS extract a session_goal, even if you can only make a rough inference.

## Session Goal

Extract a `session_goal` on EVERY turn — this is critical for context assembly.
- If this is the first turn (or no prior goal was provided), infer the goal from the user's request.
- If a prior goal was provided, update it ONLY if the user's intent has clearly changed.
- The goal should be a single sentence summarizing what the session is about.
- Examples: "Fix the login bug in the auth module", "Add dark mode to the dashboard", "Refactor the payment service"

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
  "invalidated": [],
  "session_goal": "Fix the missing json import error in src/main.py"
}

## Example Input 2 (tool-result extraction):
User: Find all Python files in the auth module
Assistant: I'll search for Python files.
[Tool result: Glob found 8 files: src/auth/__init__.py, src/auth/routes.py, src/auth/models.py, src/auth/service.py, src/auth/middleware.py, src/auth/tokens.py, src/auth/utils.py, src/auth/tests/test_routes.py]

## Example Output 2:
{
  "facts": [
   {"content": "Auth module has 8 Python files: __init__.py, routes.py, models.py, service.py, middleware.py, tokens.py, utils.py, tests/test_routes.py", "fact_type": "tool_result", "confidence": 1.0}
  ],
  "files_touched": [],
  "decisions": [],
  "invalidated": [],
  "session_goal": "Explore the auth module Python files"
}

## BAD Example (DO NOT extract like this):
User: I want to explore the frontend codebase
Assistant: Let me look at the project structure.
[Tool result: Found 14 .tsx files, 3 .css files, package.json with React 18]
BAD output: {"content": "User wants to explore yawn.frontend", "fact_type": "observation", "confidence": 0.5}
BAD output: {"content": "Assistant searched the frontend", "fact_type": "observation", "confidence": 0.5}
GOOD output: {"content": "Frontend has 14 .tsx files and 3 .css files, uses React 18", "fact_type": "tool_result", "confidence": 1.0}
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
    parts.append(f"\n{EXAMPLE_PROMPT}")
    return "\n".join(parts)
