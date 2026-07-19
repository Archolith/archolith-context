#!/usr/bin/env python3
"""One-shot proxy status and trace inspection CLI.

Commands:
    metrics         Show current proxy metrics (user_turns, assembly modes, errors)
    sessions        List trace sessions with user turn counts and cold-start progress
    turns <id>      Show per-turn detail for a session (user_turn_count, mode, tokens)
    watch [N]       Poll metrics every N seconds (default 5); Ctrl-C to stop

Usage:
    python scripts/proxy_status.py metrics
    python scripts/proxy_status.py sessions
    python scripts/proxy_status.py turns 37ef6ba0d99c4df9
    python scripts/proxy_status.py watch 3

Environment:
    PROXY_BASE_URL      Proxy URL (default: http://localhost:9800)
    PROXY_ADMIN_TOKEN   Required for /trace/* endpoints (set in .env as ADMIN_TOKEN)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path


# ── Config ──────────────────────────────────────────────────────────────────

def _load_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env loader — no dependencies."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# Load .env from project root if running from repo
_here = Path(__file__).parent.parent
_dotenv = _load_dotenv(_here / ".env")

BASE_URL = os.environ.get("PROXY_BASE_URL", _dotenv.get("PROXY_BASE_URL", "http://localhost:9800"))
ADMIN_TOKEN = os.environ.get("PROXY_ADMIN_TOKEN", _dotenv.get("ADMIN_TOKEN", ""))


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get(path: str, admin: bool = False) -> dict:
    url = BASE_URL.rstrip("/") + path
    req = urllib.request.Request(url)
    if admin and ADMIN_TOKEN:
        req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {url}: {body[:200]}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Cannot reach {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)


# ── ANSI helpers ─────────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

def _c(color: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_metrics() -> None:
    d = _get("/metrics")

    print(_c(BOLD, f"Archolith Proxy  {d.get('version', '?')}  uptime={d.get('uptime_s', '?')}s"))
    print(_c(CYAN, "  graph_ready    ") + str(d.get("graph_ready")))
    print(_c(CYAN, "  total_requests ") + str(d.get("total_requests")))

    modes = d.get("assembly_modes", {})
    # Show every non-zero mode (not just the first 4) so passthrough / agent_solo /
    # briefing / deterministic traffic is visible.
    nonzero = {k: v for k, v in modes.items() if v}
    if nonzero:
        parts = []
        for k, v in nonzero.items():
            vs = _c(MAGENTA, str(v)) if k in ("curator", "briefing") else str(v)
            parts.append(f"{k}={vs}")
        mode_str = "  ".join(parts)
    else:
        mode_str = "(all zero)"
    print(_c(CYAN, "  assembly_modes ") + mode_str)

    curator_calls = d.get("curator_calls", 0)
    curator_timeouts = d.get("curator_timeouts", 0)
    curator_fallbacks = d.get("curator_fallbacks", 0)
    if curator_calls or curator_timeouts or curator_fallbacks:
        cur_str = f"calls={curator_calls}  timeouts={_c(RED, str(curator_timeouts)) if curator_timeouts else str(curator_timeouts)}  fallbacks={curator_fallbacks}"
        print(_c(CYAN, "  curator        ") + cur_str)

    # Curator-worker diagnosis (event-driven worker + deterministic read + top-up).
    diag = d.get("curator_worker_diag")
    if diag:
        starved = diag.get("prepper_starved", 0)
        cancels = diag.get("prepper_cancels", 0)
        print(_c(CYAN, "  prepper        ")
              + f"fires={diag.get('prepper_fires', 0)}  "
              + f"starved={_c(RED, str(starved)) if starved else '0'}  "
              + f"cancels={_c(RED, str(cancels)) if cancels else '0'}  "
              + f"bg_ok={d.get('background_pass_successes', 0)}")
        det = diag.get("deterministic_assemblies", 0)
        print(_c(CYAN, "  hot_path       ")
              + f"llm_calls={diag.get('hot_path_llm_calls', 0)}  "
              + f"llm_rate={diag.get('hot_path_llm_call_rate', 0)}  "
              + f"briefing_reads={diag.get('briefing_reads', 0)}  "
              + f"avg_lag={diag.get('avg_briefing_lag_turns', 0)}  "
              + f"determin={_c(MAGENTA, str(det)) if det else '0'}")
        topups = diag.get("prepper_block_topups", 0)
        block_to = diag.get("prepper_block_timeouts", 0)
        if topups or block_to:
            print(_c(CYAN, "  sync_topup     ")
                  + f"served={topups}  timeouts={_c(RED, str(block_to)) if block_to else '0'}")

    # Helper-LLM token cost (extractor + curator + embeddings).
    helper = d.get("helper_tokens")
    if helper:
        ext = helper.get("extractor_prompt_tokens", 0) + helper.get("extractor_completion_tokens", 0)
        cur = helper.get("curator_prompt_tokens", 0) + helper.get("curator_completion_tokens", 0)
        emb = helper.get("embedding_tokens", 0)
        cached = helper.get("extractor_cached_tokens", 0) + helper.get("curator_cached_tokens", 0)
        print(_c(CYAN, "  helper_tokens  ")
              + f"extractor={ext:,}  curator={cur:,}  embed={emb:,}  cached={cached:,}")

    user_turns = d.get("user_turns_by_session", {})
    if user_turns:
        # Top 8 sessions by user-turn count; summarize the rest (avoids a wall of text).
        ranked = sorted(user_turns.items(), key=lambda kv: kv[1], reverse=True)
        shown = ranked[:8]
        rest = ranked[8:]
        line = "  ".join(f"{sid[:8]}={v}" for sid, v in shown)
        if rest:
            line += f"  (+{len(rest)} more)"
        print(_c(CYAN, "  user_turns     ") + line)
    else:
        print(_c(CYAN, "  user_turns     ") + "(no sessions)")

    errs = d.get("upstream_errors", 0)
    err_str = _c(RED, str(errs)) if errs else str(errs)
    print(_c(CYAN, "  upstream_errors") + f" {err_str}")

    successes = d.get("extraction_successes", 0)
    empties = d.get("extraction_empties", 0)
    failures = d.get("extraction_failures", 0)
    extraction_str = f"stored={successes}  empty={empties}  failed={failures}"
    print(_c(CYAN, "  extractions    ") + extraction_str)

    savings_rate = d.get("token_savings_rate", 0)
    savings_str = f"{savings_rate*100:.1f}%  ({d.get('token_savings_estimated', 0):,} tokens saved)"
    print(_c(CYAN, "  token_savings  ") + savings_str)

    print(_c(CYAN, "  trace_sessions ") + str(d.get("trace_sessions")))


def cmd_sessions() -> None:
    d = _get("/trace/sessions", admin=True)
    sessions = d.get("sessions", [])
    if not sessions:
        print("No trace sessions recorded.")
        return

    print(_c(BOLD, f"{'SESSION':<20} {'TURNS':>5} {'USER':>5} {'COLD':>5} {'GRAPH':>6} {'CURATOR':>7} {'TOKENS':>9}"))
    print("-" * 68)
    for s in sessions:
        sid = s.get("session_id", "?")
        turns = s.get("turn_count", 0)
        user_turns = s.get("max_user_turns", 0)
        modes = s.get("assembly_modes", {})
        cold = modes.get("cold_start", 0)
        graph = modes.get("graph", 0)
        curator = modes.get("curator", 0)
        tokens = s.get("total_input_tokens", 0)

        user_col = _c(GREEN if user_turns >= 3 else YELLOW, str(user_turns))
        graph_col = _c(GREEN if graph > 0 else DIM, str(graph))
        curator_col = _c(MAGENTA if curator > 0 else DIM, str(curator))
        print(f"{sid:<20} {turns:>5} {user_col:>5} {cold:>5} {graph_col:>6} {curator_col:>7} {tokens:>9,}")


def cmd_turns(session_id: str) -> None:
    d = _get(f"/trace/sessions/{session_id}", admin=True)
    summary = d.get("summary", {})
    turns = d.get("turns", [])

    print(_c(BOLD, f"Session: {session_id}"))
    modes = summary.get("assembly_modes", {})
    print(f"  turns={summary.get('turn_count')}  max_user_turns={summary.get('max_user_turns')}  "
          f"modes={dict(modes)}")
    print()

    print(_c(BOLD, f"  {'#':>3} {'MODE':<12} {'USER':>5} {'MSGS':>5} {'TOKENS':>8} {'ERR':>4}"))
    print("  " + "-" * 40)
    for t in turns:
        num = t.get("turn_number", "?")
        mode = t.get("assembly_mode", "?")
        user_n = t.get("user_turn_count", "?")
        msgs = t.get("message_count", "?")
        tokens = t.get("input_tokens", 0)
        status = t.get("upstream_status", 0)

        mode_col = _c(MAGENTA if mode == "cold_start" else GREEN, mode)
        err_col = _c(RED, str(status)) if status >= 400 else str(status)
        print(f"  {num:>3} {mode_col:<12} {user_n!s:>5} {msgs!s:>5} {tokens:>8,} {err_col:>4}")


def cmd_watch(interval: int = 5) -> None:
    print(f"Watching metrics every {interval}s — Ctrl-C to stop\n")
    try:
        while True:
            ts = time.strftime("%H:%M:%S")
            print(_c(DIM, f"── {ts} " + "─" * 40))
            cmd_metrics()
            print()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "metrics":
        cmd_metrics()
    elif cmd == "sessions":
        cmd_sessions()
    elif cmd == "turns":
        if len(args) < 2:
            print("Usage: proxy_status.py turns <session_id>", file=sys.stderr)
            sys.exit(1)
        cmd_turns(args[1])
    elif cmd == "watch":
        interval = int(args[1]) if len(args) > 1 else 5
        cmd_watch(interval)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: metrics, sessions, turns <id>, watch [N]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
