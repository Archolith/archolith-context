#!/usr/bin/env python3
"""Harness benchmark utility for archolith-proxy.

Manages the proxy side of a harness-based OpenCode benchmark:

  - Pre-generates session IDs for proxy and direct sessions
  - Sets the proxy session-ID override so the trace is keyed correctly
  - Polls the proxy trace store until the session completes
  - Builds turn records from the proxy trace (user messages + response summaries)
  - Generates an HTML comparison report using session_explorer's renderer

Harness orchestration (worktrees, sessions, comparison mode) is done by Claude
Code via MCP tools:
    harness_create_worktree  x2  (same repo, same branch, two paths)
    harness_start_session    x2  (proxy model vs direct model, autoApprove=true)
    harness_start_comparison     (opens split-pane dashboard view)

This script handles only the proxy-side concerns.

Usage:
    # Step 1 — set up before launching harness sessions
    python scripts/harness_benchmark.py setup --task "Add Redis queue to taskflow"

    # Step 2 — (Claude Code) create worktrees + start sessions

    # Step 3 — wait for completion and generate report
    python scripts/harness_benchmark.py report --session-id bench-proxy-<ts>

    # One-shot (setup + block until done + report)
    python scripts/harness_benchmark.py run --task "..." --wait-turns 20 --timeout 1800
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

_here = Path(__file__).parent.parent


def _load_dotenv(path: Path) -> dict[str, str]:
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


_dotenv = _load_dotenv(_here / ".env")

_port = os.getenv("PROXY_PORT", _dotenv.get("PROXY_PORT", "9800"))
PROXY_URL = os.getenv("PROXY_URL", f"http://localhost:{_port}/v1")
ADMIN_URL = PROXY_URL.rsplit("/v1", 1)[0]

# DeepSeek V4 Flash — registered in opencode.json under deepseek-proxy / deepseek-passthrough providers.
# Both sessions route through the proxy so token counts are recorded in the same trace store.
# The passthrough provider strips the "-passthrough" suffix and forwards unchanged (no context
# management), giving an accurate baseline for comparison. Override via env vars or .env.
PROXY_MODEL = os.getenv("BENCHMARK_PROXY_MODEL", _dotenv.get("BENCHMARK_PROXY_MODEL", "deepseek-proxy/deepseek-v4-flash"))
DIRECT_MODEL = os.getenv("BENCHMARK_DIRECT_MODEL", _dotenv.get("BENCHMARK_DIRECT_MODEL", "deepseek-passthrough/deepseek-v4-flash-passthrough"))


# ── Proxy admin API ───────────────────────────────────────────────────────────

def set_proxy_session(session_id: str) -> None:
    """Set the proxy benchmark session-ID override."""
    with httpx.Client(timeout=10) as c:
        r = c.post(
            f"{ADMIN_URL}/trace/benchmark/session-id",
            json={"session_id": session_id},
        )
        r.raise_for_status()
    print(f"  [proxy] session override set -> {session_id}")


def clear_proxy_session() -> None:
    """Clear the proxy benchmark session-ID override."""
    with httpx.Client(timeout=10) as c:
        r = c.delete(f"{ADMIN_URL}/trace/benchmark/session-id")
        r.raise_for_status()
    print("  [proxy] session override cleared")


def fetch_trace(session_id: str, retries: int = 6, delay: float = 2.0) -> dict | None:
    """Fetch the proxy trace for a session with retries."""
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=15) as c:
                r = c.get(f"{ADMIN_URL}/trace/sessions/{session_id}")
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404 and attempt < retries - 1:
                    time.sleep(delay)
                    continue
                print(f"  [trace] {r.status_code} for {session_id}", file=sys.stderr)
        except Exception as exc:
            print(f"  [trace] fetch error: {exc}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(delay)
    return None


def wait_for_trace(
    session_id: str,
    min_turns: int = 1,
    timeout_s: float = 1800,
    poll_interval: float = 10.0,
) -> dict | None:
    """Poll the proxy trace until it has at least min_turns completed, or timeout."""
    deadline = time.monotonic() + timeout_s
    print(f"  [wait] polling trace/{session_id} (min_turns={min_turns}, timeout={timeout_s}s)")
    while time.monotonic() < deadline:
        trace = fetch_trace(session_id, retries=1, delay=0)
        if trace:
            turn_count = trace.get("turn_count", 0) or len(trace.get("turns", []))
            elapsed = timeout_s - (deadline - time.monotonic())
            print(f"  [wait] {turn_count}/{min_turns} turns after {elapsed:.0f}s", end="\r", flush=True)
            if turn_count >= min_turns:
                print()
                return trace
        time.sleep(poll_interval)
    print(f"\n  [wait] timeout after {timeout_s}s")
    return fetch_trace(session_id)  # return whatever we have


# ── Turn records from trace ───────────────────────────────────────────────────

def trace_to_turn_records(trace: dict) -> list[dict]:
    """Convert proxy trace turns into the turn-record format for generate_html().

    The proxy trace has all the context management data. We reconstruct the
    user message (from original_messages) and response (from response_summary)
    so the HTML renderer has something to display.
    """
    records = []
    for i, t in enumerate(trace.get("turns", []), 1):
        # Extract last user message from original_messages
        user_msg = ""
        for msg in reversed(t.get("original_messages") or []):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                user_msg = str(content)
                break

        records.append({
            "turn": i,
            "user_msg": user_msg[:500],
            "response": t.get("response_summary", ""),
            "output_tokens": t.get("output_tokens"),
            "latency_ms": t.get("upstream_latency_ms") or t.get("assembly_latency_ms"),
            # No degraded flag for harness runs — real tool output is always valid
        })
    return records


# ── Report generation ─────────────────────────────────────────────────────────

def generate_report(
    session_id: str,
    task: str,
    trace: dict | None,
    output_path: Path | None = None,
) -> Path:
    """Generate an HTML report from the proxy trace.

    Imports generate_html from session_explorer.py (same scripts/ directory).
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from session_explorer import generate_html  # noqa: PLC0415

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)
        output_path = results_dir / f"harness_{ts}.html"

    turn_records = trace_to_turn_records(trace) if trace else []
    generate_html(
        scenario_name=task[:80] or "harness benchmark",
        session_id=session_id,
        scenario_path="(harness session)",
        turn_records=turn_records,
        trace=trace,
        output_path=output_path,
    )
    return output_path


# ── CLI commands ──────────────────────────────────────────────────────────────

def cmd_setup(args) -> None:
    """Generate IDs and set the proxy override. Print IDs for the orchestrator."""
    ts = int(time.time())
    proxy_session_id = f"bench-proxy-{ts}"
    direct_session_id = f"bench-direct-{ts}"

    print(f"\nBenchmark session IDs:")
    print(f"  proxy  : {proxy_session_id}")
    print(f"  direct : {direct_session_id}")
    print(f"\nProxy model  : {PROXY_MODEL}")
    print(f"Direct model : {DIRECT_MODEL}")
    print(f"\nTask: {args.task or '(none specified)'}")

    print("\nSetting proxy session override...")
    set_proxy_session(proxy_session_id)

    print("\nNext: Claude Code should run:")
    print(f"  harness_create_worktree  × 2  (same repo, same branch)")
    print(f"  harness_start_session(id='{proxy_session_id}', model='{PROXY_MODEL}', task=...)")
    print(f"  harness_start_session(id='{direct_session_id}', model='{DIRECT_MODEL}', task=...)")
    print(f"  harness_start_comparison(leftSessionId='{proxy_session_id}', rightSessionId='{direct_session_id}')")
    print(f"\nAfter sessions finish:")
    print(f"  python scripts/harness_benchmark.py report --session-id {proxy_session_id}")

    # Write IDs to a state file for the report step
    state_path = _here / ".bench_state.json"
    state_path.write_text(json.dumps({
        "proxy_session_id": proxy_session_id,
        "direct_session_id": direct_session_id,
        "task": args.task or "",
        "ts": ts,
    }), encoding="utf-8")
    print(f"\nState saved -> {state_path}")


def cmd_report(args) -> None:
    """Fetch the proxy trace and generate the HTML report."""
    session_id = args.session_id

    # Fall back to state file if no session ID given
    if not session_id:
        state_path = _here / ".bench_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            session_id = state.get("proxy_session_id", "")
            print(f"  [state] loaded session_id={session_id}")
        else:
            print("Error: --session-id required (no .bench_state.json found)", file=sys.stderr)
            sys.exit(1)

    task = args.task or ""
    if not task:
        state_path = _here / ".bench_state.json"
        if state_path.exists():
            task = json.loads(state_path.read_text(encoding="utf-8")).get("task", "")

    print(f"\nFetching trace for {session_id}...")
    trace = fetch_trace(session_id)
    if not trace:
        print(f"No trace found for {session_id}", file=sys.stderr)
        sys.exit(1)

    turn_count = len(trace.get("turns", []))
    print(f"  {turn_count} turns in trace")

    output_path = Path(args.output) if args.output else None
    report_path = generate_report(session_id, task, trace, output_path)
    print(f"\nReport saved -> {report_path}")
    print(f"Open: file://{report_path.resolve()}")

    # Clear the proxy override if still set
    try:
        clear_proxy_session()
    except Exception:
        pass


def cmd_run(args) -> None:
    """Setup + wait + report in one shot (blocking)."""
    # Setup
    ts = int(time.time())
    proxy_session_id = f"bench-proxy-{ts}"
    direct_session_id = f"bench-direct-{ts}"
    task = args.task or ""

    print(f"\nProxy session  : {proxy_session_id}")
    print(f"Direct session : {direct_session_id}")
    print(f"Task           : {task or '(none)'}")

    set_proxy_session(proxy_session_id)

    print("\nWaiting for proxy trace...")
    print("  (start harness sessions now if not already running)")
    trace = wait_for_trace(
        proxy_session_id,
        min_turns=args.wait_turns,
        timeout_s=args.timeout,
    )

    output_path = Path(args.output) if getattr(args, "output", None) else None
    report_path = generate_report(proxy_session_id, task, trace, output_path)
    print(f"\nReport -> {report_path}")
    print(f"Open  : file://{report_path.resolve()}")

    try:
        clear_proxy_session()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness benchmark proxy utility")
    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="Generate IDs, set proxy override, print orchestration commands")
    p_setup.add_argument("--task", default="", help="Benchmark task description")

    p_report = sub.add_parser("report", help="Fetch trace and generate HTML report")
    p_report.add_argument("--session-id", default="", help="Proxy session ID")
    p_report.add_argument("--task", default="", help="Task description (for report header)")
    p_report.add_argument("--output", default="", help="Output HTML path")

    p_run = sub.add_parser("run", help="Setup + wait for completion + generate report")
    p_run.add_argument("--task", default="", help="Benchmark task description")
    p_run.add_argument("--wait-turns", type=int, default=5, help="Min turns before considering done")
    p_run.add_argument("--timeout", type=float, default=1800, help="Timeout in seconds")
    p_run.add_argument("--output", default="", help="Output HTML path")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
