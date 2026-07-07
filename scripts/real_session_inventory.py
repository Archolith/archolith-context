"""Inventory existing trace and harness data for real-session evaluation.

This is intentionally read-only: it scans JSONL trace files and harness metadata,
classifies likely evaluation arms, and prints a Markdown summary suitable for
the real-session evaluation audit.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


EXCLUDE_PREFIXES = (
    "cb-test",
    "curator-verify",
    "gate-",
    "mob",
    "nri-test",
    "phase",
    "replay_",
    "session-",
    "smoke",
    "synlong",
    "synthtool",
    "ta-smoke",
    "test-",
    "tok-",
)

EXCLUDE_EXACT = {"__no_session__.jsonl", "curator_failures.jsonl"}


@dataclass
class TraceSummary:
    file: str
    bytes: int
    modified_at: str
    session_ids: set[str] = field(default_factory=set)
    turn_records: int = 0
    background_records: int = 0
    background_success: int = 0
    background_failed: int = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    models: Counter[str] = field(default_factory=Counter)
    modes: Counter[str] = field(default_factory=Counter)
    skip_reasons: Counter[str] = field(default_factory=Counter)
    solo_strategies: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    max_user_turns: int = 0
    max_turn_number: int = 0
    max_message_count: int = 0
    input_tokens: int = 0
    rewritten_tokens: int = 0
    savings_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    filter_saved_chars: int = 0
    curator_prompt_tokens: int = 0
    curator_completion_tokens: int = 0
    extractor_prompt_tokens: int = 0
    extractor_completion_tokens: int = 0
    embedding_tokens: int = 0
    upstream_non_200: int = 0
    harness_hint: bool = False
    original_preview: str = ""
    last_response: str = ""
    harness_id: str = ""
    harness_model: str = ""
    harness_agent: str = ""
    harness_cwd: str = ""
    harness_task: str = ""
    harness_exit: int | None = None

    @property
    def primary_session_id(self) -> str:
        if len(self.session_ids) == 1:
            return next(iter(self.session_ids))
        return Path(self.file).stem

    @property
    def arm(self) -> str:
        modes = set(self.modes)
        if {"curator", "briefing", "briefing_stale"} & modes or self.background_records:
            return "curated"
        if (
            {"agent_solo", "agent_solo_compressed"} & modes
            or self.filter_saved_chars > 0
            or bool(self.solo_strategies)
        ):
            return "mechanical"
        return "passthrough"

    @property
    def is_excluded_name(self) -> bool:
        name = self.file.lower()
        return self.file in EXCLUDE_EXACT or name.startswith(EXCLUDE_PREFIXES) or name.startswith("bench-")

    @property
    def usable_candidate(self) -> bool:
        coding_signal = self.harness_hint or self.primary_session_id.startswith("ses_") or bool(self.harness_id)
        return coding_signal and self.turn_records >= 2 and not self.is_excluded_name

    @property
    def needs_grading(self) -> bool:
        return self.usable_candidate

    @property
    def trace_days(self) -> str:
        if self.first_timestamp is None or self.last_timestamp is None:
            return ""
        first = datetime.fromtimestamp(self.first_timestamp).strftime("%Y-%m-%d")
        last = datetime.fromtimestamp(self.last_timestamp).strftime("%Y-%m-%d")
        return first if first == last else f"{first}..{last}"

    @property
    def mode_text(self) -> str:
        return ", ".join(f"{key}:{value}" for key, value in self.modes.most_common())


def _safe_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _load_harness_meta(paths: list[Path]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for base in paths:
        if not base.exists():
            continue
        for path in base.rglob("*.meta.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            data["_meta_path"] = str(path)
            for key in (data.get("agentSessionId"), data.get("id")):
                if key:
                    by_id[str(key)] = data
    return by_id


def _ingest_turn(summary: TraceSummary, obj: dict[str, Any]) -> None:
    summary.turn_records += 1
    summary.models[str(obj.get("model") or "")] += 1
    summary.modes[str(obj.get("assembly_mode") or "")] += 1
    summary.skip_reasons[str(obj.get("curator_skip_reason") or "")] += 1
    summary.max_user_turns = max(summary.max_user_turns, _safe_int(obj.get("user_turn_count")))
    summary.max_turn_number = max(summary.max_turn_number, _safe_int(obj.get("turn_number")))
    summary.max_message_count = max(summary.max_message_count, _safe_int(obj.get("message_count")))

    summary.input_tokens += _safe_int(obj.get("input_tokens"))
    summary.rewritten_tokens += _safe_int(obj.get("rewritten_tokens"))
    summary.savings_tokens += _safe_int(obj.get("savings_tokens"))
    summary.output_tokens += _safe_int(obj.get("output_tokens"))
    summary.cache_hit_tokens += _safe_int(obj.get("cache_hit_tokens"))
    summary.cache_miss_tokens += _safe_int(obj.get("cache_miss_tokens"))
    summary.filter_saved_chars += _safe_int(obj.get("filter_chars_saved"))
    summary.curator_prompt_tokens += _safe_int(obj.get("curator_prompt_tokens"))
    summary.curator_completion_tokens += _safe_int(obj.get("curator_completion_tokens"))
    summary.extractor_prompt_tokens += _safe_int(obj.get("extractor_prompt_tokens"))
    summary.extractor_completion_tokens += _safe_int(obj.get("extractor_completion_tokens"))
    summary.embedding_tokens += _safe_int(obj.get("embedding_tokens"))

    for strategy in obj.get("solo_strategies") or []:
        summary.solo_strategies[str(strategy)] += 1
    if _safe_int(obj.get("upstream_status")) not in (0, 200):
        summary.upstream_non_200 += 1
    for key in ("curator_failure_reason", "fallback_reason"):
        value = str(obj.get(key) or "")
        if value:
            summary.failures[value[:80]] += 1

    if not summary.original_preview:
        messages = obj.get("original_messages") or []
        if isinstance(messages, list):
            parts = []
            for message in messages[:2]:
                if isinstance(message, dict):
                    parts.append(str(message.get("content") or "")[:300])
            text = " ".join(parts).replace("\n", " ")
            summary.original_preview = text[:300]
            lower = text.lower()
            summary.harness_hint = any(marker in lower for marker in ("opencode", "claude code", "coding agent"))
    if obj.get("upstream_response_summary"):
        summary.last_response = str(obj.get("upstream_response_summary")).replace("\n", " ")[:240]


def _ingest_background(summary: TraceSummary, obj: dict[str, Any]) -> None:
    summary.background_records += 1
    outcome = str(obj.get("outcome") or "")
    if outcome == "success":
        summary.background_success += 1
    elif outcome:
        summary.background_failed += 1


def summarize_trace(path: Path, harness_meta: dict[str, dict[str, Any]]) -> tuple[TraceSummary, int]:
    stat = path.stat()
    summary = TraceSummary(
        file=path.name,
        bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
    )
    parse_errors = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            session_id = obj.get("session_id")
            if session_id:
                summary.session_ids.add(str(session_id))
            timestamp = obj.get("created_at") or obj.get("started_at")
            if isinstance(timestamp, (int, float)):
                summary.first_timestamp = timestamp if summary.first_timestamp is None else min(summary.first_timestamp, timestamp)
                summary.last_timestamp = timestamp if summary.last_timestamp is None else max(summary.last_timestamp, timestamp)
            if obj.get("record_type") == "bg_pass":
                _ingest_background(summary, obj)
            else:
                _ingest_turn(summary, obj)

    for key in (summary.primary_session_id, Path(summary.file).stem, *summary.session_ids):
        meta = harness_meta.get(key)
        if not meta:
            continue
        summary.harness_id = str(meta.get("id") or "")
        summary.harness_model = str(meta.get("model") or "")
        summary.harness_agent = str(meta.get("agent") or "")
        summary.harness_cwd = str(meta.get("cwd") or "")
        summary.harness_task = str(meta.get("task") or "").replace("\n", " ")[:160]
        exit_code = meta.get("exitCode")
        summary.harness_exit = int(exit_code) if isinstance(exit_code, int) else None
        break
    return summary, parse_errors


def markdown_report(summaries: list[TraceSummary], parse_errors: int, harness_meta_count: int) -> str:
    usable = [s for s in summaries if s.usable_candidate]
    lines = [
        "# Real-Session Trace Inventory",
        "",
        f"- Trace files scanned: {len(summaries)}",
        f"- Harness metadata records indexed: {harness_meta_count}",
        f"- JSONL parse errors: {parse_errors}",
        f"- Usable candidates needing session-grade: {len(usable)}",
        f"- Usable candidates by arm: {dict(Counter(s.arm for s in usable))}",
        f"- All traces by arm: {dict(Counter(s.arm for s in summaries))}",
        "",
        "## Usable Candidate Sessions",
        "",
        "| Trace | Arm | Turns | User turns | Max messages | Input tokens | Savings tokens | Cache hit/miss | Filter chars saved | Modes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in sorted(usable, key=lambda s: (s.turn_records, s.input_tokens), reverse=True):
        lines.append(
            "| {trace} | {arm} | {turns} | {user_turns} | {messages} | {input_tokens} | {savings} | {cache} | {filter_saved} | {modes} |".format(
                trace=item.file,
                arm=item.arm,
                turns=item.turn_records,
                user_turns=item.max_user_turns,
                messages=item.max_message_count,
                input_tokens=item.input_tokens,
                savings=item.savings_tokens,
                cache=f"{item.cache_hit_tokens}/{item.cache_miss_tokens}",
                filter_saved=item.filter_saved_chars,
                modes=item.mode_text,
            )
        )
    lines.extend(
        [
            "",
            "## Excluded Trace Buckets",
            "",
            "| Reason | Count | Examples |",
            "|---|---:|---|",
        ]
    )
    excluded = [s for s in summaries if not s.usable_candidate]
    buckets: dict[str, list[str]] = {}
    for item in excluded:
        if item.file in EXCLUDE_EXACT:
            reason = "aggregate/failure log"
        elif item.is_excluded_name:
            reason = "synthetic/smoke/benchmark naming"
        elif item.turn_records < 2:
            reason = "too few trace turns"
        else:
            reason = "no coding harness signal"
        buckets.setdefault(reason, []).append(item.file)
    for reason, files in sorted(buckets.items()):
        lines.append(f"| {reason} | {len(files)} | {', '.join(files[:5])} |")
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
    summaries: list[TraceSummary] = []
    parse_errors = 0
    for trace_path in sorted(args.trace_dir.glob("*.jsonl")):
        summary, errors = summarize_trace(trace_path, harness_meta)
        summaries.append(summary)
        parse_errors += errors

    print(markdown_report(summaries, parse_errors, len(harness_meta)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
