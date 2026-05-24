"""OpenAI-compatible tool schemas for the curator's 10 tools.

Each schema follows the OpenAI function-calling format used by
delegate_server.py ALL_TOOL_LIST. These are passed as the `tools=`
parameter when calling the curator LLM.
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
                "Do NOT include coherence tail turns — they are always kept automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "turn_numbers": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Turn numbers to retain (use the t-numbers from the turn "
                            "inventory, e.g. [3, 5, 8]). Empty list means drop all "
                            "middle turns."
                        ),
                    },
                },
                "required": ["turn_numbers"],
            },
        },
    },
]
