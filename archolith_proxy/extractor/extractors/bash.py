"""Specialized extractor for Bash tool results."""

from __future__ import annotations

from typing import Any, Dict


def extract_bash_tool_result(tool_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract structured information from a Bash tool result.

    Expected input:
        {
            "command": "...",
            "exit_code": 0,
            "output": "...",
            "description": "..."
        }
    """
    command = tool_result.get("command", "")
    exit_code = tool_result.get("exit_code", 0)
    output = tool_result.get("output", "") or tool_result.get("stdout", "")

    success = exit_code == 0

    # Simple error detection
    errors = []
    if not success:
        errors.append(f"Command failed with exit code {exit_code}")

    # Detect common test patterns
    summary = ""
    if "pytest" in command.lower() or "test" in command.lower():
        if "passed" in output.lower():
            summary = "Tests passed"
        elif "failed" in output.lower():
            summary = "Tests failed"

    return {
        "command": command,
        "exit_code": exit_code,
        "success": success,
        "errors": errors,
        "summary": summary or ("Command succeeded" if success else "Command failed"),
    }