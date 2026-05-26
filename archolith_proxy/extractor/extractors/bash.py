"""BashExtractor — regex pre-pass first, LLM call only if regex doesn't cover it."""

from __future__ import annotations

import json
import re

import httpx
import structlog

from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord, ToolExtractor
from archolith_proxy.extractor.prompts import BASH_SYSTEM_PROMPT, build_bash_extraction_prompt
from archolith_proxy.config import get_settings

logger = structlog.get_logger()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ENV_VAR_RE = re.compile(r"^[A-Z_]+=\S*$")

# Shell builtins that should fall through to LLM immediately
_SHELL_BUILTINS = frozenset({"source", "export", "cd", "unset", "alias", "eval", "set", "exit"})

# Regex patterns keyed by command prefix
_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {
    "pytest": [
        (re.compile(r"(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
        (re.compile(r"(\d+) warning"), "tests_warnings"),
        (re.compile(r"FAILED\s+([\w/.:]+)"), "failed_test"),
    ],
    "python": [  # python -m pytest
        (re.compile(r"(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
        (re.compile(r"(\d+) warning"), "tests_warnings"),
        (re.compile(r"FAILED\s+([\w/.:]+)"), "failed_test"),
    ],
    "npm": [
        (re.compile(r"Tests:\s+(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
        (re.compile(r"FAIL\s+(\S+)"), "failed_test"),
    ],
    "jest": [
        (re.compile(r"Tests:\s+(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
        (re.compile(r"FAIL\s+(\S+)"), "failed_test"),
    ],
    "vitest": [
        (re.compile(r"Tests:\s+(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
        (re.compile(r"FAIL\s+(\S+)"), "failed_test"),
    ],
    "cargo": [
        (re.compile(r"test result: (ok|FAILED)"), "test_result"),
        (re.compile(r"(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
    ],
    "go": [  # go test
        (re.compile(r"test result: (ok|FAIL)"), "test_result"),
        (re.compile(r"(\d+) passed"), "tests_passed"),
        (re.compile(r"(\d+) failed"), "tests_failed"),
    ],
    "git": [],  # filled below with sub-dispatch
}

# Git sub-command patterns
_GIT_STATUS_RE = re.compile(r"^\s+(?:modified|new file|deleted):\s+(.+)", re.MULTILINE)
_GIT_DIFF_FILE_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:a/|b/)(.+)", re.MULTILINE)
_GIT_LOG_RE = re.compile(r"^([0-9a-f]{7,}) (.+)", re.MULTILINE)

# Universal error pattern
_ERROR_RE = re.compile(r"(?:error|Error|ERROR):?\s+(.{0,120})")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _classify_command(command: str) -> str:
    """Extract the primary command from a possibly compound/piped command string.

    Returns the primary tool name for pattern matching, or a shell builtin
    name for immediate LLM fallthrough.
    """
    # Strip leading env-var assignments
    tokens = command.split()
    first_real_idx = 0
    for i, token in enumerate(tokens):
        if _ENV_VAR_RE.match(token):
            first_real_idx = i + 1
            continue
        break

    if first_real_idx >= len(tokens):
        return ""

    primary = tokens[first_real_idx]

    # For compound commands (&&, ||, ;), classify on the first segment
    for sep in ("&&", "||", ";"):
        if sep in primary:
            # The first segment before the separator
            primary = primary.split(sep)[0].strip()
            break

    # For pipes, the primary is the left side
    if "|" in primary:
        primary = primary.split("|")[0].strip()

    # Shell builtins → immediate fallthrough to LLM
    base = primary.split()[-1] if primary else ""
    if base in _SHELL_BUILTINS or primary in _SHELL_BUILTINS:
        return "builtin"

    # Extract the command name (first token)
    cmd_name = primary.split()[0] if primary.split() else primary

    # Handle "python -m pytest" style invocations
    if cmd_name == "python" and "-m" in primary:
        parts = primary.split()
        try:
            m_idx = parts.index("-m")
            if m_idx + 1 < len(parts):
                cmd_name = parts[m_idx + 1]
        except (ValueError, IndexError):
            pass

    return cmd_name


class BashExtractor(ToolExtractor):
    """Handles Bash tool calls — regex first, LLM fallback."""

    tool_names = ("Bash",)

    async def extract(
        self,
        record: ToolCallRecord,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        command = record.args.get("command", "")
        output = _strip_ansi(record.result)

        # Classify command
        cmd_name = _classify_command(command)

        # Shell builtin or unrecognizable → fall through to LLM immediately
        if cmd_name == "builtin" or not cmd_name:
            return await self._llm_extract(record, command, output, http_client, turn_number, session_goal)

        # Try regex patterns
        facts = self._apply_regex(cmd_name, command, output)

        if facts:
            return PartialExtractionResult(
                source_tool="Bash",
                facts=facts,
                files_touched=self._extract_git_files(cmd_name, command, output),
                used_llm=False,
            )

        # No regex match → LLM fallback
        return await self._llm_extract(record, command, output, http_client, turn_number, session_goal)

    def _apply_regex(self, cmd_name: str, command: str, output: str) -> list[dict]:
        """Apply regex patterns for the classified command. Returns empty list if no match."""
        facts = []

        if cmd_name == "git":
            # Sub-dispatch by git subcommand
            tokens = command.split()
            subcmd = ""
            for i, t in enumerate(tokens):
                if t == "git" and i + 1 < len(tokens):
                    subcmd = tokens[i + 1]
                    break

            if subcmd == "status":
                for match in _GIT_STATUS_RE.findall(output):
                    facts.append({
                        "content": f"[Bash] git status: {match.strip()}",
                        "fact_type": "tool_result",
                        "confidence": 1.0,
                    })
            elif subcmd == "diff":
                for match in _GIT_DIFF_FILE_RE.findall(output):
                    facts.append({
                        "content": f"[Bash] git diff file: {match.strip()}",
                        "fact_type": "tool_result",
                        "confidence": 1.0,
                    })
            elif subcmd == "log":
                for commit_hash, msg in _GIT_LOG_RE.findall(output):
                    facts.append({
                        "content": f"[Bash] git log: {commit_hash} {msg.strip()}",
                        "fact_type": "tool_result",
                        "confidence": 1.0,
                    })
            else:
                # Generic git → try error pattern
                pass
        else:
            patterns = _PATTERNS.get(cmd_name, [])
            for pattern, kind in patterns:
                for match in pattern.findall(output):
                    if kind in ("tests_passed", "tests_failed", "tests_warnings"):
                        facts.append({
                            "content": f"[Bash] {cmd_name}: {match} {kind.replace('tests_', '')}",
                            "fact_type": "tool_result",
                            "confidence": 1.0,
                        })
                    elif kind == "failed_test":
                        facts.append({
                            "content": f"[Bash] {cmd_name} FAILED: {match}",
                            "fact_type": "error",
                            "confidence": 1.0,
                        })
                    elif kind == "test_result":
                        facts.append({
                            "content": f"[Bash] {cmd_name} test result: {match}",
                            "fact_type": "tool_result",
                            "confidence": 1.0,
                        })

        # Universal error pattern (always applied)
        for err_msg in _ERROR_RE.findall(output):
            facts.append({
                "content": f"[Bash] error: {err_msg.strip()}",
                "fact_type": "error",
                "confidence": 0.9,
            })

        return facts

    def _extract_git_files(self, cmd_name: str, command: str, output: str) -> list[str]:
        """Extract file paths from git commands for files_touched."""
        if cmd_name != "git":
            return []
        tokens = command.split()
        subcmd = ""
        for i, t in enumerate(tokens):
            if t == "git" and i + 1 < len(tokens):
                subcmd = tokens[i + 1]
                break
        if subcmd == "status":
            return [m.strip() for m in _GIT_STATUS_RE.findall(output)]
        if subcmd == "diff":
            return [m.strip() for m in _GIT_DIFF_FILE_RE.findall(output)]
        return []

    async def _llm_extract(
        self,
        record: ToolCallRecord,
        command: str,
        output: str,
        http_client: httpx.AsyncClient,
        turn_number: int,
        session_goal: str | None,
    ) -> PartialExtractionResult:
        """LLM fallback for Bash output that regex couldn't parse."""
        settings = get_settings()
        user_prompt = build_bash_extraction_prompt(command, output[:4000], turn_number)

        payload = {
            "model": settings.extractor_model,
            "messages": [
                {"role": "system", "content": BASH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1000,
        }

        try:
            resp = await http_client.post(
                f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.extractor_api_key}",
                    "Content-Type": "application/json",
                },
                content=json.dumps(payload).encode(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())

            facts = parsed.get("facts", [])
            verifications = parsed.get("verifications", [])

            # Prefix facts with [Bash]
            prefixed_facts = []
            for f in facts:
                if isinstance(f, dict):
                    f["content"] = f"[Bash] {f.get('content', '')}"
                    prefixed_facts.append(f)
                elif isinstance(f, str):
                    prefixed_facts.append({"content": f"[Bash] {f}", "fact_type": "tool_result", "confidence": 0.7})

            # Include verifications as facts with appropriate type
            for v in verifications:
                if isinstance(v, dict) and v.get("command"):
                    prefixed_facts.append({
                        "content": f"[Bash] verification: {v.get('command', '')} — {v.get('status', 'unknown')}: {v.get('summary', '')}",
                        "fact_type": "state",
                        "confidence": 0.9,
                    })

            return PartialExtractionResult(
                source_tool="Bash",
                facts=prefixed_facts,
                files_touched=[],
                used_llm=True,
            )
        except Exception as e:
            logger.warning("bash_extractor_llm_failed", error=str(e))
            return PartialExtractionResult(
                source_tool="Bash",
                facts=[{
                    "content": f"[Bash] {command[:100]}: {output[:200]}",
                    "fact_type": "tool_result",
                    "confidence": 0.4,
                }],
                files_touched=[],
                used_llm=True,
            )
