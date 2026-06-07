"""Replay a captured real session through the proxy to get a true baseline.

The synthetic benchmark scenarios are user/assistant text only — no tool
messages — so the proxy never enters the agent-solo/RTK path and the curator has
nothing to assemble (every turn is passthrough). This replays a REAL exported
session (scripts/opencode_export.py) by sending each growing message prefix to
the proxy under a pinned X-Session-ID, so the proxy accumulates the session and
its curation machinery actually engages. We then read the proxy trace and report
what it did (assembly mode, savings, RTK firing, curator skip reasons).

Output tokens are capped low — we care about what the proxy DID to the context,
not the model's reply. Sends real upstream calls (budget); use --max-turns.

Usage:
    python scripts/replay_session.py --fixture scripts/captured/<f>.json --max-turns 30
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

import httpx

PROXY = "http://127.0.0.1:9800"


def load_prefixes(fixture: Path) -> list[list[dict]]:
    """Reconstruct the agent's request sequence: each prefix ending just before
    an assistant message (i.e., ending in a user or tool message)."""
    data = json.loads(fixture.read_text(encoding="utf-8"))
    messages = data["messages"] if isinstance(data, dict) else data
    prefixes: list[list[dict]] = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and i > 0:
            prefix = messages[:i]
            if prefix and prefix[-1].get("role") in ("user", "tool"):
                prefixes.append(prefix)
    return prefixes


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay a captured session through the proxy")
    ap.add_argument("--fixture", type=Path, required=True)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--max-tokens", type=int, default=64, help="Cap reply size (cost control)")
    ap.add_argument("--session-id", default=None, help="Pin X-Session-ID (default: generated)")
    args = ap.parse_args()

    session_id = args.session_id or f"replay_{int(time.time())}"
    prefixes = load_prefixes(args.fixture)
    if args.max_turns:
        prefixes = prefixes[: args.max_turns]
    print(f"Replaying {len(prefixes)} turns as session {session_id}")

    headers = {"Content-Type": "application/json", "X-Session-ID": session_id}
    with httpx.Client(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as client:
        # health gate
        h = client.get(f"{PROXY}/health").json()
        if h.get("graph") != "connected":
            print(f"ERROR: proxy graph not connected: {h}", file=sys.stderr)
            sys.exit(1)

        for n, prefix in enumerate(prefixes, 1):
            body = {"model": args.model, "messages": prefix,
                    "max_tokens": args.max_tokens, "temperature": 0.0, "stream": False}
            t0 = time.monotonic()
            try:
                r = client.post(f"{PROXY}/v1/chat/completions", headers=headers, json=body)
            except httpx.HTTPError as e:
                print(f"  turn {n}: HTTP error {e}", file=sys.stderr)
                break
            ms = (time.monotonic() - t0) * 1000
            last_role = prefix[-1]["role"]
            if r.status_code == 429:
                print(f"  turn {n}: 429 RATE LIMITED — stopping.", file=sys.stderr)
                break
            if r.status_code != 200:
                print(f"  turn {n}: status {r.status_code}: {r.text[:200]}", file=sys.stderr)
                break
            print(f"  turn {n:3d}/{len(prefixes)}  last={last_role:9s}  {len(prefix):3d} msgs  {ms:6.0f}ms")
            time.sleep(0.3)  # let trace flush

        # ── pull trace + report ──
        time.sleep(3)
        det = client.get(f"{PROXY}/trace/sessions/{session_id}").json()
        turns = det.get("turns", [])
        modes = collections.Counter(t.get("assembly_mode") for t in turns)
        skips = collections.Counter((t.get("curator_skip_reason") or "(none)") for t in turns)
        rtk_avail = sum(1 for t in turns if t.get("rtk_available"))
        rtk_saved = sum(t.get("rtk_chars_saved", 0) for t in turns)
        recalls = sum(1 for t in turns if t.get("recall_used"))
        solo = sum(1 for t in turns if t.get("assembly_mode") == "agent_solo")

    print("\n" + "=" * 60)
    print(f"REPLAY BASELINE — session {session_id} ({len(turns)} recorded turns)")
    print("=" * 60)
    print("assembly modes:", dict(modes))
    print("curator skip reasons:", dict(skips))
    print(f"rtk_available turns: {rtk_avail}/{len(turns)}  | rtk_chars_saved total: {rtk_saved:,}")
    print(f"agent_solo turns: {solo}  | recall_used turns: {recalls}/{len(turns)}")


if __name__ == "__main__":
    main()
