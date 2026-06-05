"""Export a real OpenCode session into an OpenAI-format message array.

OpenCode stores a session as message rows (role) whose content lives in ordered
``part`` rows. A ``tool`` part bundles the call AND result
(``{tool, callID, state:{input, output}}``). This script flattens that into a
standard OpenAI chat array so a real tool-using coding session can be analyzed
(scripts/redundancy.py) or replayed as a benchmark fixture — the synthetic
scenarios have no tool messages and cannot exercise RTK/agent-solo/curator.

Each OpenCode tool part becomes two OpenAI messages:
  assistant {tool_calls:[{id:callID, function:{name:tool, arguments:input}}]}
  tool      {tool_call_id:callID, name:tool, content:output}

Usage:
    python scripts/opencode_export.py --session ses_XXXX --out fixture.json
    python scripts/opencode_export.py --list          # recent sessions
    python scripts/opencode_export.py --session ses_X --redundancy   # + analyze
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode-dev.db"

# Part types that carry no conversational content for replay purposes.
_SKIP_PART_TYPES = frozenset({"step-start", "step-finish", "reasoning", "compaction", "patch"})


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def list_sessions(con: sqlite3.Connection, limit: int = 20) -> None:
    q = """
        select s.id, substr(s.title,1,48) title, s.directory,
               count(m.id) msgs, s.time_updated
        from session s left join message m on m.session_id = s.id
        group by s.id order by s.time_updated desc limit ?
    """
    for r in con.execute(q, (limit,)):
        d = (r["directory"] or "")[-26:]
        print(f"  {r['id']:<30} msgs={r['msgs']:<4} ...{d:<26} {(r['title'] or '')[:42]}")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def export_session(con: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """Flatten one OpenCode session into an OpenAI-format message list."""
    messages: list[dict[str, Any]] = []

    msg_rows = con.execute(
        "select id, data from message where session_id = ? order by id",
        (session_id,),
    ).fetchall()

    for msg in msg_rows:
        mdata = json.loads(msg["data"])
        role = mdata.get("role")

        parts = con.execute(
            "select data from part where message_id = ? order by id",
            (msg["id"],),
        ).fetchall()

        text_chunks: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for p in parts:
            pdata = json.loads(p["data"])
            ptype = pdata.get("type")
            if ptype in _SKIP_PART_TYPES:
                continue
            if ptype == "text":
                txt = pdata.get("text")
                if txt:
                    text_chunks.append(txt)
            elif ptype == "tool":
                call_id = pdata.get("callID") or pdata.get("id") or ""
                name = pdata.get("tool") or ""
                state = pdata.get("state") or {}
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(state.get("input") or {}, ensure_ascii=False),
                    },
                })
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _stringify(state.get("output")),
                })

        content = "\n".join(text_chunks)

        if role == "user":
            messages.append({"role": "user", "content": content})
        elif role == "assistant":
            asst: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                asst["tool_calls"] = tool_calls
            messages.append(asst)
            messages.extend(tool_results)  # each tool result follows the assistant call
        # other roles (system, etc.) are rare in OpenCode storage; skip silently

    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an OpenCode session to OpenAI message JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"OpenCode DB (default: {DEFAULT_DB})")
    parser.add_argument("--list", action="store_true", help="List recent sessions and exit")
    parser.add_argument("--session", help="Session id (ses_...) to export")
    parser.add_argument("--out", type=Path, help="Output JSON path (default: stdout)")
    parser.add_argument("--redundancy", action="store_true", help="Also print a read-file redundancy report")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    con = _connect(args.db)

    if args.list:
        list_sessions(con)
        return

    if not args.session:
        parser.error("provide --session <id> or --list")

    messages = export_session(con, args.session)
    if not messages:
        print(f"ERROR: no messages for session {args.session}", file=sys.stderr)
        sys.exit(1)

    payload = {"session_id": args.session, "messages": messages}

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Wrote {len(messages)} messages to {args.out}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))

    if args.redundancy:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import redundancy
        report = redundancy.classify_read_redundancy(messages)
        print()
        print(redundancy.format_report(report, title=args.session))


if __name__ == "__main__":
    main()
