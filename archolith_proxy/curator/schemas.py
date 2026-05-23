"""OpenAI-compatible tool schemas for the curator's 7 tools.

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
]
