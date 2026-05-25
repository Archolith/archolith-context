"""Scripted harness benchmark with gated filesystem verification.

Runs a dual-session comparison (proxy vs passthrough) through the archolith-proxy
infrastructure. Sessions execute in isolated git worktrees. Filesystem checkpoints
provide objective pass/fail signals — gated failures terminate the offending session.

Usage (orchestrated by Claude Code / harness operator):
    python scripts/scripted_benchmark.py setup --scenario scripts/scenarios/harness/config_doc.json
    python scripts/scripted_benchmark.py start --proxy-worktree <path> --passthrough-worktree <path>
    python scripts/scripted_benchmark.py monitor
    python scripts/scripted_benchmark.py report
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path

import httpx

# ── .env loader (no python-dotenv dependency) ─────────────────────────────────

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

_here = Path(__file__).parent.parent
_dotenv = _load_dotenv(_here / ".env")

PROXY_URL = os.getenv("PROXY_URL", _dotenv.get("PROXY_URL", "http://localhost:9801"))
PROXY_BASE = PROXY_URL.rstrip("/").removesuffix("/v1")
HARNESS_URL = os.getenv("HARNESS_URL", _dotenv.get("HARNESS_URL", "http://localhost:3485"))
STATE_FILE = _here / ".scripted_bench_state.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------

def _send_harness_handoff(session_id: str, message: str) -> bool:
    """Send a follow-up user message to a running harness session via MCP.

    Returns True on success, False on error.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "harness_send_handoff",
            "arguments": {
                "targetSessionId": session_id,
                "senderName": "benchmark-orchestrator",
                "message": message,
            },
        },
    }
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(
                f"{HARNESS_URL}/mcp",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json=payload,
            )
            # MCP returns SSE — extract first data line
            text = r.text
            for line in text.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    parsed = json.loads(data)
                    if "error" in parsed:
                        print(f"HANDOFF_ERROR session={session_id} error={parsed['error']}")
                        return False
                    return True
            return r.status_code < 400
    except Exception as e:
        print(f"HANDOFF_FAILED session={session_id} error={e}")
        return False


# ---------------------------------------------------------------------------
# Filesystem checks
# ---------------------------------------------------------------------------

def run_filesystem_check(worktree_path: Path, check: dict) -> tuple[bool, str]:
    """Run a single filesystem check. Returns (passed, failure_description)."""
    target = worktree_path / check["path"]

    if "exists" in check:
        if not target.is_file():
            return False, f"{check['path']}:not_found"

    if "contains" in check:
        if not target.is_file():
            return False, f"{check['path']}:not_found"
        text = target.read_text(encoding="utf-8", errors="replace")
        if check["contains"] not in text:
            return False, f"{check['path']}:missing_content:{check['contains'][:40]!r}"

    if check.get("not_empty"):
        if not target.is_file() or target.stat().st_size == 0:
            return False, f"{check['path']}:empty"

    if "min_lines" in check:
        if not target.is_file():
            return False, f"{check['path']}:not_found"
        count = target.read_text(encoding="utf-8", errors="replace").count("\n")
        if count < check["min_lines"]:
            return False, f"{check['path']}:min_lines:{count}<{check['min_lines']}"

    return True, ""


def run_checkpoint_checks(
    worktree_path: Path, checks: list[dict]
) -> tuple[bool, list[str]]:
    """Run all filesystem checks for a checkpoint. Returns (all_passed, failed_list)."""
    failed: list[str] = []
    for check in checks:
        passed, desc = run_filesystem_check(worktree_path, check)
        if not passed:
            failed.append(desc)
    return len(failed) == 0, failed


# ---------------------------------------------------------------------------
# Trace fetching
# ---------------------------------------------------------------------------

def fetch_trace(session_id: str) -> dict | None:
    """Fetch proxy trace for a session."""
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(f"{PROXY_BASE}/trace/sessions/{session_id}")
            return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def load_state(path: Path | None = None) -> dict:
    p = path or STATE_FILE
    if not p.exists():
        print(f"ERROR: State file not found: {p}", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or STATE_FILE
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Phase 1: setup
# ---------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace) -> None:
    scenario_path = Path(args.scenario).resolve()
    if not scenario_path.exists():
        print(f"ERROR: Scenario file not found: {scenario_path}", file=sys.stderr)
        sys.exit(1)

    with open(scenario_path) as f:
        scenario = json.load(f)

    # Validate required fields
    for field in ("name", "task_prompt", "checkpoints"):
        if field not in scenario:
            print(f"ERROR: Scenario missing required field: {field}", file=sys.stderr)
            sys.exit(1)

    ts = int(time.time())
    proxy_sid = f"bench-proxy-{ts}"
    pass_sid = f"bench-pass-{ts}"

    # Register both session IDs so proxy traces both paths under their benchmark IDs
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"{PROXY_BASE}/trace/benchmark/session-id",
                json={"session_id": proxy_sid, "passthrough_session_id": pass_sid},
            )
            if r.status_code not in (200, 201, 204):
                print(f"WARNING: trace/benchmark/session-id returned {r.status_code}")
    except Exception as e:
        print(f"WARNING: Could not set proxy trace session: {e}")

    state = {
        "scenario": scenario["name"],
        "scenario_file": str(scenario_path),
        "proxy_session_id": proxy_sid,
        "passthrough_session_id": pass_sid,
        "proxy_model": args.proxy_model or "deepseek-proxy/deepseek-v4-flash",
        "passthrough_model": args.passthrough_model or "deepseek-passthrough/deepseek-v4-flash-passthrough",
        "task_prompt": scenario["task_prompt"],
        "follow_up_turns": scenario.get("follow_up_turns", []),
        "checkpoints": scenario["checkpoints"],
        "total_timeout_seconds": scenario.get("total_timeout_seconds", 600),
        "ts": ts,
        "started_at": None,
        "proxy_worktree": None,
        "passthrough_worktree": None,
        "proxy_terminated_at": None,
        "passthrough_terminated_at": None,
        "checkpoint_results": [],
    }

    save_state(state, Path(args.state) if args.state else None)

    # Print key=value output for orchestrator
    print(f"PROXY_SESSION_ID={proxy_sid}")
    print(f"PASSTHROUGH_SESSION_ID={pass_sid}")
    print(f"PROXY_MODEL={state['proxy_model']}")
    print(f"PASSTHROUGH_MODEL={state['passthrough_model']}")
    print(f"TASK_PROMPT_FILE={STATE_FILE}")
    print(f"STATE_FILE={STATE_FILE}")


# ---------------------------------------------------------------------------
# Phase 2: start
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state) if args.state else None)

    # Verify proxy is reachable and record baseline uptime
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{PROXY_BASE}/health")
            uptime = float(r.json().get("uptime_s", 0)) if r.status_code == 200 else None
    except Exception:
        uptime = None

    if uptime is None:
        print("ERROR: Proxy is not reachable — start the proxy before running the benchmark", file=sys.stderr)
        sys.exit(1)

    state["started_at"] = time.time()
    state["proxy_baseline_uptime"] = uptime
    state["proxy_worktree"] = str(Path(args.proxy_worktree).resolve())
    state["passthrough_worktree"] = str(Path(args.passthrough_worktree).resolve())

    save_state(state, Path(args.state) if args.state else None)
    print(f"STARTED proxy_worktree={state['proxy_worktree']}")
    print(f"        passthrough_worktree={state['passthrough_worktree']}")
    print(f"        started_at={state['started_at']}")
    print(f"        proxy_baseline_uptime={uptime:.0f}s")


# ---------------------------------------------------------------------------
# Phase 3: monitor
# ---------------------------------------------------------------------------

def _get_proxy_uptime() -> float | None:
    """Return proxy uptime_s from /health, or None on failure."""
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{PROXY_BASE}/health")
            if r.status_code == 200:
                return float(r.json().get("uptime_s", 0))
    except Exception:
        pass
    return None


def cmd_monitor(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state) if args.state else None)

    started_at = state["started_at"]
    if not started_at:
        print("ERROR: started_at is null — did you run 'start'?", file=sys.stderr)
        sys.exit(1)

    proxy_wt = Path(state["proxy_worktree"])
    pass_wt = Path(state["passthrough_worktree"])

    checkpoints = state["checkpoints"]
    total_timeout = state.get("total_timeout_seconds", 600)
    proxy_dead = state.get("proxy_terminated_at") is not None
    pass_dead = state.get("passthrough_terminated_at") is not None
    checkpoint_results: list[dict] = state.get("checkpoint_results", [])

    # Follow-up turns — inject additional user messages at scheduled elapsed times
    follow_up_turns = state.get("follow_up_turns", [])
    follow_up_sent: list[bool] = [False] * len(follow_up_turns)
    proxy_session_id = state["proxy_session_id"]
    pass_session_id = state["passthrough_session_id"]

    # Use baseline uptime from cmd_start (preferred) or re-fetch now
    baseline_uptime = state.get("proxy_baseline_uptime")
    if baseline_uptime is None:
        baseline_uptime = _get_proxy_uptime()
    if baseline_uptime is None:
        print("ERROR: Proxy is not reachable at monitor start — aborting", file=sys.stderr)
        sys.exit(1)
    print(f"PROXY_BASELINE_UPTIME={baseline_uptime:.0f}s")

    for cp in checkpoints:
        cp_id = cp["id"]
        wait_seconds = cp["check_after_seconds"]
        gate = cp.get("gate", False)
        fs_checks = cp["filesystem_checks"]

        # Send any follow-up turns that are due before this checkpoint's check window
        for i, turn in enumerate(follow_up_turns):
            if follow_up_sent[i]:
                continue
            turn_due_at = turn["send_after_seconds"]
            elapsed = time.time() - started_at
            if elapsed < turn_due_at:
                sleep_until_turn = turn_due_at - elapsed
                if sleep_until_turn > 0:
                    print(f"WAIT follow_up_turn={i+1} sleeping {sleep_until_turn:.0f}s (send_after={turn_due_at}s)")
                    time.sleep(sleep_until_turn)
            # Send to both sessions
            msg = turn["message"]
            if not proxy_dead:
                ok = _send_harness_handoff(proxy_session_id, msg)
                print(f"TURN_SENT proxy turn={i+2} ok={ok}")
            if not pass_dead:
                ok = _send_harness_handoff(pass_session_id, msg)
                print(f"TURN_SENT passthrough turn={i+2} ok={ok}")
            follow_up_sent[i] = True

        # Wait until the check window
        elapsed = time.time() - started_at
        remaining = wait_seconds - elapsed
        if remaining > 0:
            print(f"WAIT checkpoint={cp_id} sleeping {remaining:.0f}s (check_after={wait_seconds}s)")
            time.sleep(remaining)

        # Poll every 10 seconds until checks pass or timeout
        poll_start = time.time()
        max_poll = (total_timeout - wait_seconds) if gate else 60
        proxy_passed = False
        pass_passed = False
        proxy_failed_checks: list[str] = []
        pass_failed_checks: list[str] = []
        infra_failure: str | None = None

        while True:
            elapsed = time.time() - started_at
            timed_out = elapsed > total_timeout

            # Check for proxy restart (uptime would be < elapsed since monitor start)
            current_uptime = _get_proxy_uptime()
            if current_uptime is not None and current_uptime < baseline_uptime - 5:
                infra_failure = (
                    f"PROXY_RESTARTED baseline={baseline_uptime:.0f}s current={current_uptime:.0f}s"
                )
                print(f"INFRA_FAIL {infra_failure}")
                # Update baseline so subsequent polls don't re-trigger
                baseline_uptime = current_uptime

            # Always run filesystem checks before deciding to break
            if not proxy_dead:
                proxy_passed, proxy_failed_checks = run_checkpoint_checks(proxy_wt, fs_checks)
            if not pass_dead:
                pass_passed, pass_failed_checks = run_checkpoint_checks(pass_wt, fs_checks)

            if timed_out:
                print(f"TIMEOUT total_timeout={total_timeout}s exceeded at checkpoint={cp_id}")
                break

            # Both passed or both dead — done with this checkpoint
            if (proxy_passed or proxy_dead) and (pass_passed or pass_dead):
                break

            # At least one still failing — keep polling
            if time.time() - poll_start > max_poll:
                break

            time.sleep(10)

        result = {
            "checkpoint": cp_id,
            "elapsed_seconds": round(time.time() - started_at, 1),
            "proxy": proxy_passed,
            "passthrough": pass_passed,
            "proxy_failed": proxy_failed_checks if not proxy_passed else [],
            "passthrough_failed": pass_failed_checks if not pass_passed else [],
        }
        if infra_failure:
            result["infra_failure"] = infra_failure
        checkpoint_results.append(result)
        print(json.dumps(result))

        # Abort entire benchmark on infrastructure failure
        if infra_failure:
            print(f"BENCHMARK_ABORTED reason=infra_failure detail={infra_failure!r}")
            state["checkpoint_results"] = checkpoint_results
            state["infra_failure"] = infra_failure
            save_state(state, Path(args.state) if args.state else None)
            sys.exit(2)

        # Handle gate failures
        if gate:
            if not proxy_passed and not proxy_dead:
                print(f"GATE_FAIL proxy {cp_id}")
                state["proxy_terminated_at"] = cp_id
                proxy_dead = True
            if not pass_passed and not pass_dead:
                print(f"GATE_FAIL passthrough {cp_id}")
                state["passthrough_terminated_at"] = cp_id
                pass_dead = True

        # Save incremental state
        state["checkpoint_results"] = checkpoint_results
        save_state(state, Path(args.state) if args.state else None)

        # Both dead — no point continuing
        if proxy_dead and pass_dead:
            print("BOTH_TERMINATED")
            break

    print("MONITOR_DONE")


# ---------------------------------------------------------------------------
# Phase 4: report
# ---------------------------------------------------------------------------

def cmd_report(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state) if args.state else None)

    proxy_sid = state["proxy_session_id"]
    pass_sid = state["passthrough_session_id"]
    scenario_name = state["scenario"]
    ts = state["ts"]

    # Fetch traces
    proxy_trace = fetch_trace(proxy_sid)
    pass_trace = fetch_trace(pass_sid)

    # Extract metrics from traces
    def extract_trace_metrics(trace: dict | None) -> dict:
        if not trace:
            return {"input_tokens": 0, "output_tokens": 0, "turns": 0,
                    "savings_tokens": 0, "savings_ratio": 0.0, "assembly_modes": {}}
        summary = trace.get("summary", {})
        turns = trace.get("turns", [])
        modes: dict[str, int] = {}
        total_savings = 0
        for t in turns:
            mode = t.get("assembly_mode", "unknown")
            modes[mode] = modes.get(mode, 0) + 1
            total_savings += t.get("savings_tokens", 0)
        return {
            "input_tokens": summary.get("total_input_tokens", 0),
            "output_tokens": summary.get("total_output_tokens", 0),
            "turns": summary.get("total_turns", len(turns)),
            "savings_tokens": total_savings,
            "savings_ratio": round(total_savings / max(summary.get("total_input_tokens", 1), 1), 4),
            "assembly_modes": modes,
        }

    proxy_metrics = extract_trace_metrics(proxy_trace)
    pass_metrics = extract_trace_metrics(pass_trace)

    # Build report data
    report_data = {
        "scenario": scenario_name,
        "ts": ts,
        "proxy_session_id": proxy_sid,
        "passthrough_session_id": pass_sid,
        "checkpoints": state.get("checkpoint_results", []),
        "proxy_terminated_at": state.get("proxy_terminated_at"),
        "passthrough_terminated_at": state.get("passthrough_terminated_at"),
        "proxy_turns": proxy_metrics["turns"],
        "passthrough_turns": pass_metrics["turns"],
        "proxy_input_tokens": proxy_metrics["input_tokens"],
        "passthrough_input_tokens": pass_metrics["input_tokens"],
        "proxy_savings_tokens": proxy_metrics["savings_tokens"],
        "proxy_savings_ratio": proxy_metrics["savings_ratio"],
        "proxy_assembly_modes": proxy_metrics["assembly_modes"],
    }

    # Ensure results dir exists
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # Write JSON sidecar
    json_path = RESULTS_DIR / f"scripted_{scenario_name}_{ts}.json"
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2)
    print(f"JSON={json_path}")

    # Write HTML report
    html_path = RESULTS_DIR / f"scripted_{scenario_name}_{ts}.html"
    html_content = _generate_html_report(report_data, state)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"REPORT={html_path}")


def _generate_html_report(report_data: dict, state: dict) -> str:
    """Generate a self-contained HTML report."""
    scenario = report_data["scenario"]
    checkpoints = report_data["checkpoints"]

    def status_badge(passed: bool) -> str:
        color = "#22c55e" if passed else "#ef4444"
        label = "PASS" if passed else "FAIL"
        return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:12px">{label}</span>'

    # Checkpoint table rows
    cp_rows = ""
    for cp in checkpoints:
        cp_rows += f"""<tr>
            <td>{html.escape(cp['checkpoint'])}</td>
            <td>{status_badge(cp['proxy'])}</td>
            <td>{status_badge(cp['passthrough'])}</td>
            <td>{html.escape(', '.join(cp.get('proxy_failed', []))) or '&mdash;'}</td>
            <td>{html.escape(', '.join(cp.get('passthrough_failed', []))) or '&mdash;'}</td>
            <td>{cp['elapsed_seconds']:.1f}s</td>
        </tr>"""

    # Termination info
    proxy_term = html.escape(report_data["proxy_terminated_at"] or "completed")
    pass_term = html.escape(report_data["passthrough_terminated_at"] or "completed")

    # Assembly modes
    modes = report_data.get("proxy_assembly_modes", {})
    modes_rows = ""
    for mode, count in sorted(modes.items()):
        modes_rows += f"<tr><td>{html.escape(mode)}</td><td>{count}</td></tr>"

    # Savings calculation
    proxy_in = report_data["proxy_input_tokens"]
    pass_in = report_data["passthrough_input_tokens"]
    savings_pct = report_data["proxy_savings_ratio"]
    savings_tok = report_data["proxy_savings_tokens"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Scripted Benchmark: {html.escape(scenario)}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 2rem; background: #0f172a; color: #e2e8f0; }}
h1 {{ color: #38bdf8; }}
h2 {{ color: #818cf8; margin-top: 2rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #334155; padding: 8px 12px; text-align: left; }}
th {{ background: #1e293b; color: #94a3b8; }}
td {{ background: #1e293b; }}
.metric {{ font-size: 2rem; font-weight: bold; }}
.label {{ color: #94a3b8; font-size: 0.875rem; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 1rem 0; }}
.card {{ background: #1e293b; border-radius: 8px; padding: 1rem; }}
code {{ background: #334155; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>Scripted Benchmark Report</h1>
<p>Scenario: <strong>{html.escape(scenario)}</strong> &middot; Timestamp: {html.escape(str(report_data['ts']))}</p>

<h2>1. Task Prompt</h2>
<pre style="background:#1e293b;padding:1rem;border-radius:8px;overflow-x:auto;white-space:pre-wrap;font-size:0.85rem;">{html.escape(state.get('task_prompt', ''))}</pre>

<h2>2. Checkpoint Results</h2>
<table>
<tr><th>Checkpoint</th><th>Proxy</th><th>Passthrough</th><th>Proxy Failures</th><th>Passthrough Failures</th><th>Elapsed</th></tr>
{cp_rows}
</table>

<h2>3. Termination</h2>
<div class="grid">
<div class="card"><div class="label">Proxy</div><div class="metric" style="color:{'#22c55e' if report_data['proxy_terminated_at'] is None else '#ef4444'}">{proxy_term}</div></div>
<div class="card"><div class="label">Passthrough</div><div class="metric" style="color:{'#22c55e' if report_data['passthrough_terminated_at'] is None else '#ef4444'}">{pass_term}</div></div>
</div>

<h2>4. Token Comparison</h2>
<div class="grid">
<div class="card"><div class="label">Proxy Input Tokens</div><div class="metric">{proxy_in:,}</div></div>
<div class="card"><div class="label">Passthrough Input Tokens</div><div class="metric">{pass_in:,}</div></div>
<div class="card"><div class="label">Proxy Turns</div><div class="metric">{report_data['proxy_turns']}</div></div>
<div class="card"><div class="label">Passthrough Turns</div><div class="metric">{report_data['passthrough_turns']}</div></div>
</div>

<h2>5. Proxy Savings</h2>
<div class="grid">
<div class="card"><div class="label">Tokens Saved</div><div class="metric" style="color:#22c55e">{savings_tok:,}</div></div>
<div class="card"><div class="label">Savings Ratio</div><div class="metric" style="color:#22c55e">{savings_pct:.1%}</div></div>
</div>
{"<h3>Assembly Modes</h3><table><tr><th>Mode</th><th>Count</th></tr>" + modes_rows + "</table>" if modes_rows else ""}

<hr style="border-color:#334155;margin-top:2rem;">
<p style="color:#64748b;font-size:0.75rem;">
Proxy session: <code>{html.escape(report_data['proxy_session_id'])}</code> &middot;
Passthrough session: <code>{html.escape(report_data['passthrough_session_id'])}</code>
</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scripted harness benchmark with gated filesystem verification",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Initialize benchmark state and session IDs")
    p_setup.add_argument("--scenario", required=True, help="Path to scenario JSON file")
    p_setup.add_argument("--proxy-model", default=None, help="Proxy model identifier")
    p_setup.add_argument("--passthrough-model", default=None, help="Passthrough model identifier")
    p_setup.add_argument("--state", default=None, help="Path to state file")

    # start
    p_start = sub.add_parser("start", help="Record worktree paths and start timestamp")
    p_start.add_argument("--proxy-worktree", required=True, help="Proxy session worktree path")
    p_start.add_argument("--passthrough-worktree", required=True, help="Passthrough session worktree path")
    p_start.add_argument("--state", default=None, help="Path to state file")

    # monitor
    p_monitor = sub.add_parser("monitor", help="Poll worktrees for checkpoint completion")
    p_monitor.add_argument("--state", default=None, help="Path to state file")

    # report
    p_report = sub.add_parser("report", help="Generate HTML and JSON reports")
    p_report.add_argument("--state", default=None, help="Path to state file")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "monitor":
        cmd_monitor(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
