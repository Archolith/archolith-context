"""Analyze a captured OpenAI-format multi-turn message list
and classify the tokens spent on FILE-READ tool results into three buckets:
exact-duplicate, superseded (by a later full-file write), and live.

Usage:
    python scripts/redundancy.py session.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

READ_TOOLS: frozenset[str] = frozenset({
    "read", "read_file", "cat", "head", "tail",
    "get_file", "get_file_lines", "view", "open",
})

FULL_WRITE_TOOLS: frozenset[str] = frozenset({
    "write", "write_file", "create_file", "create", "str_replace_editor",
})
# NOTE: partial-edit tools (edit, edit_file, patch, str_replace) are
# deliberately NOT in FULL_WRITE_TOOLS — a partial edit does NOT supersede
# a prior read.


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough character-based token estimate (matches scripts/benchmark.py convention)."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Tool-call ID resolution helpers
# ---------------------------------------------------------------------------

def _build_call_map(messages: list[dict]) -> dict[str, tuple[str, str]]:
    """Scan assistant messages for tool_calls and return {call_id: (tool_name, path)}.

    tool_name is lowercased; path is extracted from the JSON-decoded arguments
    (looks for ``file_path`` first, then ``path``).  Fails-open to empty string
    on any parse error.
    """
    call_map: dict[str, tuple[str, str]] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        try:
            if msg.get("role") != "assistant":
                continue
            calls = msg.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for tc in calls:
                if not isinstance(tc, dict):
                    continue
                call_id = tc.get("id")
                if not call_id:
                    continue
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                tool_name = str(fn.get("name", "")).lower()
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                path: str = str(args.get("file_path") or args.get("path") or "")
                call_map[call_id] = (tool_name, path)
        except Exception:
            continue
    return call_map


def _resolve_tool_result(msg: dict, call_map: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Resolve (tool_name_lower, path) for a tool-result message.

    Uses ``tool_call_id`` to look up the call map; falls back to
    ``msg.get("name")`` for the name and ``""`` for path.
    """
    try:
        call_id = msg.get("tool_call_id", "")
        if call_id in call_map:
            return call_map[call_id]
    except Exception:
        pass
    name = str(msg.get("name", "")).lower() if isinstance(msg.get("name"), str) else ""
    return name, ""


# ---------------------------------------------------------------------------
# ReadEvent
# ---------------------------------------------------------------------------

@dataclass
class ReadEvent:
    """A single file-read tool result that was observed in the session."""

    index: int  # position in the messages list
    path: str
    tool: str
    content: str
    tokens: int


def extract_read_events(messages: list[dict]) -> list[ReadEvent]:
    """Yield a ReadEvent for every tool-result whose resolved tool is a READ_TOOL.

    Messages that are malformed or have empty/non-string content are skipped.
    """
    call_map = _build_call_map(messages)
    events: list[ReadEvent] = []
    for idx, msg in enumerate(messages):
        try:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            tool_name, path = _resolve_tool_result(msg, call_map)
            if tool_name not in READ_TOOLS:
                continue
            events.append(
                ReadEvent(
                    index=idx,
                    path=path,
                    tool=tool_name,
                    content=content,
                    tokens=estimate_tokens(content),
                )
            )
        except Exception:
            continue
    return events


# ---------------------------------------------------------------------------
# Full-write index detection
# ---------------------------------------------------------------------------

def find_full_write_indices(messages: list[dict]) -> dict[str, list[int]]:
    """Map path -> sorted list of message indices where a completed full-file write occurred.

    A completed write is an assistant tool_call whose function.name (lowered)
    is in FULL_WRITE_TOOLS AND whose id appears as a tool_call_id on some later
    tool-result message (i.e. it was actually executed) AND whose parsed args
    path is non-empty.

    Returns a dict keyed by path; values are sorted lists of message indices
    (the index of the *assistant* message that *issued* the call).
    """
    # First, collect the set of executed tool_call_ids
    executed_ids: set[str] = set()
    for msg in messages:
        try:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if isinstance(cid, str) and cid:
                    executed_ids.add(cid)
        except Exception:
            continue

    writes: dict[str, list[int]] = {}

    for idx, msg in enumerate(messages):
        try:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            calls = msg.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for tc in calls:
                if not isinstance(tc, dict):
                    continue
                call_id = tc.get("id", "")
                if call_id not in executed_ids:
                    continue
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue
                tool_name = str(fn.get("name", "")).lower()
                if tool_name not in FULL_WRITE_TOOLS:
                    continue
                # Parse arguments for path
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                path: str = str(args.get("file_path") or args.get("path") or "")
                if not path:
                    continue
                writes.setdefault(path, []).append(idx)
        except Exception:
            continue

    # Sort each list
    for path in writes:
        writes[path].sort()
    return writes


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@dataclass
class RedundancyReport:
    """Result of classifying reads into redundancy buckets."""

    total_read_tokens: int = 0
    exact_dup_tokens: int = 0
    superseded_tokens: int = 0
    live_tokens: int = 0
    total_reads: int = 0
    exact_dup_reads: int = 0
    superseded_reads: int = 0
    live_reads: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a dict that includes ratio fields (0..1 rounded to 4 dp)."""
        total = self.total_read_tokens
        exact_dup_ratio = round(self.exact_dup_tokens / total, 4) if total else 0.0
        superseded_ratio = round(self.superseded_tokens / total, 4) if total else 0.0
        live_ratio = round(self.live_tokens / total, 4) if total else 0.0
        return {
            "total_read_tokens": self.total_read_tokens,
            "exact_dup_tokens": self.exact_dup_tokens,
            "superseded_tokens": self.superseded_tokens,
            "live_tokens": self.live_tokens,
            "total_reads": self.total_reads,
            "exact_dup_reads": self.exact_dup_reads,
            "superseded_reads": self.superseded_reads,
            "live_reads": self.live_reads,
            "exact_dup_ratio": exact_dup_ratio,
            "superseded_ratio": superseded_ratio,
            "live_ratio": live_ratio,
        }


def classify_read_redundancy(messages: list[dict]) -> RedundancyReport:
    """Classify read events into exact-dup / superseded / live buckets.

    **Precedence** (each read falls into exactly one bucket):

    1. **EXACT_DUP** — content is byte-identical to an *earlier* read event
       (any path).  The first occurrence of a given content is never a dup.
    2. **SUPERSEDED** — not a dup, and there exists a full-write index *j* for
       the same path where *j* > read.index.
    3. **LIVE** — everything else.
    """
    reads = extract_read_events(messages)
    writes = find_full_write_indices(messages)

    report = RedundancyReport()
    seen_contents: set[str] = set()

    for r in reads:
        report.total_reads += 1
        report.total_read_tokens += r.tokens

        # --- Exact dup check (precedence #1) ---
        if r.content in seen_contents:
            report.exact_dup_reads += 1
            report.exact_dup_tokens += r.tokens
            continue

        seen_contents.add(r.content)

        # --- Superseded check (precedence #2) ---
        write_indices = writes.get(r.path, [])
        if any(j > r.index for j in write_indices):
            report.superseded_reads += 1
            report.superseded_tokens += r.tokens
            continue

        # --- Live (precedence #3) ---
        report.live_reads += 1
        report.live_tokens += r.tokens

    return report


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_report(report: RedundancyReport, *, title: str = "") -> str:
    """Human-readable multi-line summary of the redundancy report."""
    lines: list[str] = []
    if title:
        lines.append(title)
        lines.append("")

    lines.append(f"Total reads: {report.total_reads:>4}, tokens: {report.total_read_tokens:>7}")
    lines.append("")

    def _bucket_line(label: str, tokens: int, cnt: int) -> str:
        pct = (tokens / report.total_read_tokens * 100) if report.total_read_tokens else 0.0
        return f"  {label:20s} {tokens:>7} tok ({pct:>5.1f}%) across {cnt} reads"

    lines.append(_bucket_line("exact-dup:", report.exact_dup_tokens, report.exact_dup_reads))
    lines.append(_bucket_line("superseded:", report.superseded_tokens, report.superseded_reads))
    lines.append(_bucket_line("live:", report.live_tokens, report.live_reads))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze file-read redundancy in an OpenAI-format session JSON."
    )
    parser.add_argument(
        "session_json",
        type=str,
        help="Path to a JSON file containing messages (list) or an object with a 'messages' key.",
    )
    args = parser.parse_args()

    path = Path(args.session_json)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Accept either a raw list or an object with a "messages" key
    if isinstance(raw, list):
        messages = raw
    elif isinstance(raw, dict):
        messages = raw.get("messages")
        if messages is None:
            print(
                "ERROR: JSON object does not contain a 'messages' key",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(messages, list):
            print(
                "ERROR: 'messages' value is not a list",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print(
            "ERROR: JSON must be a list (messages) or an object with a 'messages' key",
            file=sys.stderr,
        )
        sys.exit(1)

    report = classify_read_redundancy(messages)
    print(format_report(report, title=path.name))


if __name__ == "__main__":
    main()
