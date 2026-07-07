"""Triage real-session candidates before manual/session-grade review.

The inventory script answers "what traces might be real sessions?"  This script
answers "which of those traces have enough task/outcome signal to grade now?"
It does not assign final product grades.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from real_session_inventory import _load_harness_meta, summarize_trace


GENERIC_USER_PREFIXES = (
    "generate a title for this conversation",
    "show open plans",
    "sho open plans",
    "check open plans",
)

SUCCESS_MARKERS = (
    "done",
    "created",
    "committed",
    "tests passed",
    "all checks passed",
    "successfully",
    "green",
)

FAILURE_MARKERS = (
    "traceback",
    "failed",
    "error",
    "couldn't",
    "cannot",
    "blocked",
    "permission denied",
)

RESEARCH_TASK_MARKERS = (
    "explore",
    "search",
    "understand",
    "study",
    "find and return",
    "return:",
    "summarize",
    "summary",
)

REPORT_OUTCOME_MARKERS = (
    "comprehensive summary",
    "summary of my findings",
    "complete summary",
    "complete pattern analysis",
    "search results",
    "here is a complete summary",
    "here is the complete",
)

COMPLETION_MARKERS = (
    "definition of done checklist",
    "validation passed",
    "commit ",
    "committed",
    "wrapup",
)

HARNESS_BLOCKER_MARKERS = (
    "permission required",
    "access external directory",
    "model deepseek-proxy/deepseek-v4-flash is not valid",
)

TASK_FILE_RE = re.compile(r"TASK-([A-Za-z0-9_.-]+)\.md")


@dataclass(frozen=True)
class Gradeability:
    trace: str
    arm: str
    turns: int
    user_turns: int
    input_tokens: int
    savings_tokens: int
    task_seed: str
    harness_id: str
    harness_exit: int | None
    last_user: str
    last_response: str
    meaningful_user_messages: int
    success_markers: int
    failure_markers: int
    status: str
    reason: str


def _shorten(text: str, limit: int = 140) -> str:
    clean = " ".join(text.replace("|", "/").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _meaningful_user_text(text: str) -> bool:
    stripped = " ".join(text.lower().split())
    if len(stripped) < 8:
        return False
    return not any(stripped.startswith(prefix) for prefix in GENERIC_USER_PREFIXES)


def _marker_count(text: str, markers: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for marker in markers if marker in lower)


def _turn_objects(path: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("record_type") != "bg_pass":
                turns.append(obj)
    return turns


def _task_refs(messages: list[str]) -> list[str]:
    refs: list[str] = []
    for message in messages:
        for match in TASK_FILE_RE.finditer(message):
            refs.append(match.group(1))
    return refs


def _harness_meta_for_task_refs(
    harness_meta: dict[str, dict[str, Any]], task_refs: list[str]
) -> dict[str, Any] | None:
    for task_ref in task_refs:
        meta = harness_meta.get(task_ref)
        if meta:
            return meta
    return None


def _harness_log_contains(meta: dict[str, Any], markers: tuple[str, ...]) -> bool:
    meta_path_text = meta.get("_meta_path")
    if not meta_path_text:
        return False
    clean_log = Path(str(meta_path_text)).with_suffix("").with_suffix(".clean.log")
    if not clean_log.exists():
        return False
    try:
        text = clean_log.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in text for marker in markers)


def _unique_user_messages(turns: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    messages: list[str] = []
    for turn in turns:
        for message in turn.get("original_messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = str(message.get("content") or "")
            key = " ".join(text.split())
            if not key or key in seen:
                continue
            seen.add(key)
            if _meaningful_user_text(text):
                messages.append(text)
    return messages


def assess_trace(path: Path, summary, harness_meta: dict[str, dict[str, Any]]) -> Gradeability:
    turns = _turn_objects(path)
    meaningful_users = _unique_user_messages(turns)
    external_meta = _harness_meta_for_task_refs(harness_meta, _task_refs(meaningful_users))
    harness_id = str((external_meta or {}).get("id") or summary.harness_id or "")
    exit_code = (external_meta or {}).get("exitCode", summary.harness_exit)
    harness_exit = int(exit_code) if isinstance(exit_code, int) else None
    response_candidates = [
        str(turn.get("upstream_response_summary") or "")
        for turn in turns
        if turn.get("upstream_response_summary")
    ]
    last_response = response_candidates[-1] if response_candidates else ""
    failure_blob = " ".join(
        str(turn.get("curator_failure_reason") or "") + " " + str(turn.get("fallback_reason") or "")
        for turn in turns
    )
    response_blob = f"{' '.join(response_candidates)} {failure_blob}"
    task_blob = " ".join(meaningful_users)
    success_markers = _marker_count(response_blob, SUCCESS_MARKERS)
    failure_markers = _marker_count(response_blob, FAILURE_MARKERS)
    research_task_markers = _marker_count(task_blob, RESEARCH_TASK_MARKERS)
    report_outcome_markers = _marker_count(response_blob, REPORT_OUTCOME_MARKERS)
    completion_markers = _marker_count(response_blob, COMPLETION_MARKERS)
    harness_blocked = bool(
        external_meta
        and harness_exit not in (None, 0)
        and completion_markers == 0
        and _harness_log_contains(external_meta, HARNESS_BLOCKER_MARKERS)
    )

    if not meaningful_users:
        status = "evidence-gap"
        reason = "no non-generic user task recovered from trace"
    elif not last_response:
        status = "evidence-gap"
        reason = "no final response summary recovered"
    elif harness_blocked:
        status = "harness-blocked"
        reason = "external harness log shows permission/model gate before outcome"
    elif failure_markers and not success_markers:
        status = "manual-review"
        reason = "failure markers present; inspect task outcome before grading"
    elif research_task_markers and report_outcome_markers:
        status = "ready-to-grade"
        reason = "research/report task with summary outcome signal"
    elif len(meaningful_users) >= 2 or success_markers:
        status = "ready-to-grade"
        reason = "task and outcome signals present"
    else:
        status = "manual-review"
        reason = "single task signal; needs outcome confirmation"

    return Gradeability(
        trace=path.name,
        arm=summary.arm,
        turns=summary.turn_records,
        user_turns=summary.max_user_turns,
        input_tokens=summary.input_tokens,
        savings_tokens=summary.savings_tokens,
        task_seed=_shorten(meaningful_users[0] if meaningful_users else ""),
        harness_id=harness_id,
        harness_exit=harness_exit,
        last_user=_shorten(meaningful_users[-1] if meaningful_users else ""),
        last_response=_shorten(last_response),
        meaningful_user_messages=len(meaningful_users),
        success_markers=success_markers,
        failure_markers=failure_markers,
        status=status,
        reason=reason,
    )


def markdown_report(items: list[Gradeability]) -> str:
    lines = [
        "# Real-Session Gradeability Triage",
        "",
        f"- Candidate sessions assessed: {len(items)}",
        f"- Status counts: {dict(Counter(item.status for item in items))}",
        f"- Ready by arm: {dict(Counter(item.arm for item in items if item.status == 'ready-to-grade'))}",
        "",
        "## Ready To Grade",
        "",
        "| Trace | Arm | Turns | User msgs | Task seed | Last response signal |",
        "|---|---:|---:|---:|---|---|",
    ]
    for item in [x for x in items if x.status == "ready-to-grade"]:
        lines.append(
            f"| `{item.trace}` | {item.arm} | {item.turns} | {item.meaningful_user_messages} | "
            f"{item.task_seed} | {item.last_response} |"
        )

    lines.extend(
        [
            "",
            "## Manual Review / Evidence Gaps",
            "",
            "| Trace | Arm | Status | Reason | Task seed | Last response signal |",
            "|---|---:|---|---|---|---|",
        ]
    )
    for item in [x for x in items if x.status != "ready-to-grade"]:
        harness_suffix = ""
        if item.harness_id:
            harness_suffix = f" ({item.harness_id}, exit {item.harness_exit})"
        lines.append(
            f"| `{item.trace}` | {item.arm} | {item.status} | {item.reason}{harness_suffix} | "
            f"{item.task_seed} | {item.last_response} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, default=Path("data/traces"))
    parser.add_argument(
        "--harness-log-dir",
        type=Path,
        action="append",
        default=[
            Path(r"C:\Users\thron\IdeaProjects\projects\ctharvey\cth.harness\session-logs"),
            Path(r"C:\Users\thron\IdeaProjects\projects\ctharvey\cth.harness\session-logs\archive"),
        ],
    )
    args = parser.parse_args()

    harness_meta = _load_harness_meta(args.harness_log_dir)
    items: list[Gradeability] = []
    for trace_path in sorted(args.trace_dir.glob("*.jsonl")):
        summary, _ = summarize_trace(trace_path, harness_meta)
        if summary.usable_candidate:
            items.append(assess_trace(trace_path, summary, harness_meta))

    items.sort(key=lambda item: (item.status != "ready-to-grade", -item.turns, item.trace))
    print(markdown_report(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
