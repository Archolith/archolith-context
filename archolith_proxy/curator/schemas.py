"""OpenAI-compatible tool schemas for the curator's tools.

Each schema follows the OpenAI function-calling format used by
delegate_server.py ALL_TOOL_LIST. These are passed as the `tools=`
parameter when calling the curator LLM.

Three tool sets are defined:
- ALL_CURATOR_TOOLS — full set for the default single-bot curator
- PREPPER_TOOLS — all tools + score_file_relevance for the background prepper
- ASSEMBLER_TOOLS — minimal set (select_relevant_turns + get_file_lines) for the inline assembler
"""

from __future__ import annotations

ALL_CURATOR_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_session_files",
            "description": "List all cached files for the current session. Returns a table of path, line count, and last-updated turn.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file",
            "description": "Get the full content of a cached file. For files over 200 lines, returns only the first 10 lines plus a hint to use get_file_lines for specific sections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path as it appears in list_session_files",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_outline",
            "description": (
                "Get the structural outline of a cached file — all functions, classes, and "
                "async functions with their line numbers. Call this BEFORE get_file_lines on "
                "any file over 100 lines to identify the exact line range you need without "
                "reading the full content. Returns 'line N: def/class <name>' entries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path as it appears in list_session_files",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_lines",
            "description": "Retrieve specific line range from cached file content. Prefer this over get_file for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path as it appears in list_session_files",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to return (1-indexed)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to return (inclusive)",
                    },
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_facts",
            "description": "Search active facts by keyword substring match. Returns a bullet list of matching facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for in active facts",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_facts_semantic",
            "description": (
                "Search active facts by semantic similarity — finds facts conceptually "
                "related to the query even when they share no keywords. Use this when "
                "search_facts returns nothing or when the question uses different "
                "terminology than the stored facts (e.g. 'JWT expiry' finding "
                "'token TTL' facts). Falls back to substring matching if embeddings "
                "are unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language phrase describing what to find",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of facts to return (default 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_goal",
            "description": "Get the session goal string — the overall task the coding agent is working on.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_decisions",
            "description": "Get recent decisions recorded during the session as a numbered list with turn numbers.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_touched_files",
            "description": "Get all files touched (read, modified, created, deleted) in the session as a table.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
},
    {
        "type": "function",
        "function": {
            "name": "get_checkpoint",
            "description": (
                "Get the current work checkpoint: what state the session is in right now "
                "and what the next step is. Call this first — it is the fastest way to "
                "orient to where the session stands."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_issues",
            "description": (
                "Get all open (unresolved) issues for the session: errors, blockers, "
                "failing tests, and unresolved problems. Includes the file and command "
                "associated with each issue when available."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_last_verification",
            "description": (
                "Get the most recent test or verification result: the exact command run, "
                "whether it passed/failed/partial, and a summary of what was tested. "
                "Use this when the current question involves tests, builds, or commands."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_relevant_turns",
            "description": (
                "Select which historical conversation turns to retain in the compressed "
                "context. Turns NOT selected will be dropped (their key facts are preserved "
                "in the system context block above). Call this after reviewing the turn "
                "inventory in the user prompt. Include turns whose conversations introduced "
                "patterns, schemas, or decisions still active in the current question. "
                "Omit turns fully captured in facts/checkpoint. "
                "Do NOT include coherence tail turns — they are always kept automatically. "
                "Order the list by relevance: most relevant to the current question FIRST."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "turn_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Turn numbers to retain, ordered by relevance to the current "
                            "question (most relevant first). Example: [5, 3, 8] means t5 "
                            "is most relevant, t8 least. Empty list = drop all middle turns."
                        ),
                    },
                },
                "required": ["turn_numbers"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prefetch_file",
            "description": (
                "Read a file from the local filesystem and cache it for this session. "
                "Use this to proactively load files the agent will likely need — imports "
                "of files being edited, test files, configs referenced in decisions. "
                "The file will then be available via get_file and get_file_lines on "
                "subsequent turns. Prefer absolute paths. Returns a preview of the "
                "cached content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File path to read and cache. Absolute paths work directly. "
                            "Relative paths are resolved against existing cached file locations."
                        ),
                    },
                    "focus": {
                        "type": "string",
                        "description": (
                            "Optional: description of the section you need (e.g. 'the auth "
                            "handler function', 'class TraceStore'). When provided, returns "
                            "the structural outline plus the focused section (~80 lines max) "
                            "instead of the full file. Omit to cache the entire file."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
]

# Prepper tool set — all curator tools plus score_file_relevance
SCORE_FILE_RELEVANCE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "score_file_relevance",
        "description": (
            "Score all cached files by relevance to a given query. Returns a ranked list "
            "of files with relevance scores and reasons. Use this to identify which files "
            "the next question is likely to need, so you can pre-fetch their outlines and "
            "key sections. Saves multiple list_session_files + get_file_outline calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The likely next question or topic — what the developer's next turn "
                        "is expected to be about. Natural language description."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

PREPPER_TOOLS: list[dict] = [*ALL_CURATOR_TOOLS, SCORE_FILE_RELEVANCE_SCHEMA]

# Assembler tool set — minimal: just select_relevant_turns + get_file_lines
def _find_tool(name: str, tools: list[dict]) -> dict | None:
    """Find a tool schema by name in a tool list."""
    for t in tools:
        if t.get("function", {}).get("name") == name:
            return t
    return None

_ASSEMBLER_TOOL_NAMES = {"select_relevant_turns", "get_file_lines"}
ASSEMBLER_TOOLS: list[dict] = [
    t for t in ALL_CURATOR_TOOLS
    if t.get("function", {}).get("name") in _ASSEMBLER_TOOL_NAMES
]

__all__ = ["ALL_CURATOR_TOOLS", "PREPPER_TOOLS", "ASSEMBLER_TOOLS"]
