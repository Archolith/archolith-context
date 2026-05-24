#!/usr/bin/env python3
"""Archolith Context Explorer — run a scenario and generate a visual HTML report.

Runs N turns of a scenario through the proxy, collects the session trace, and
produces a self-contained HTML file showing exactly what the proxy did on each
turn: assembly mode, context composition, curator decisions, token economics,
and response quality.

Usage:
    python scripts/session_explorer.py
    python scripts/session_explorer.py --turns 12 --scenario scripts/scenarios/debugging.json
    python scripts/session_explorer.py --output my_session.html

The generated HTML is completely self-contained (no external deps) and can be
opened directly in a browser or shared.
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

_port = os.getenv("PROXY_PORT", _dotenv.get("PROXY_PORT", "9801"))
PROXY_URL = os.getenv("PROXY_URL", f"http://localhost:{_port}/v1")
ADMIN_URL = PROXY_URL.rsplit("/v1", 1)[0]
MODEL = os.getenv("BENCHMARK_MODEL", _dotenv.get("BENCHMARK_MODEL", "deepseek-chat"))
MAX_TOKENS = int(os.getenv("EXPLORER_MAX_TOKENS", "4096"))


# ── Runner ────────────────────────────────────────────────────────────────────

def run_scenario(scenario_path: Path, n_turns: int, session_id: str) -> list[dict]:
    """Run N turns of the scenario through the proxy. Returns list of turn records."""
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    turns_spec = scenario.get("turns", [])[:n_turns]
    scenario_name = scenario.get("name", scenario_path.stem)
    print(f"Scenario : {scenario_name}")
    print(f"Session  : {session_id}")
    print(f"Turns    : {len(turns_spec)}")
    print(f"Proxy    : {PROXY_URL}")
    print(f"Model    : {MODEL}")
    print()

    # Circuit breaker: if output_tokens stays below this threshold for
    # CIRCUIT_BREAKER_CONSECUTIVE consecutive turns, the model is stuck
    # (tool-call artifacts, empty responses, context collapse) — stop early.
    CIRCUIT_BREAKER_MIN_TOKENS = int(os.getenv("EXPLORER_MIN_TOKENS", "100"))
    CIRCUIT_BREAKER_CONSECUTIVE = int(os.getenv("EXPLORER_BREAKER_TURNS", "3"))
    low_token_streak = 0

    messages: list[dict] = []
    records: list[dict] = []

    with httpx.Client(timeout=180) as client:
        for i, turn_spec in enumerate(turns_spec, 1):
            # Scenario turns can be plain strings or dicts with a "user" key
            if isinstance(turn_spec, str):
                user_msg = turn_spec
            else:
                user_msg = turn_spec.get("user", turn_spec.get("user_message", ""))
            messages.append({"role": "user", "content": user_msg})

            print(f"  [{i:>2}/{len(turns_spec)}] ", end="", flush=True)
            t0 = time.monotonic()
            try:
                resp = client.post(
                    f"{PROXY_URL}/chat/completions",
                    json={
                        "model": MODEL,
                        "messages": messages,
                        "max_tokens": MAX_TOKENS,
                        "temperature": 0.7,
                    },
                    headers={"X-Session-ID": session_id},
                )
                resp.raise_for_status()
                data = resp.json()
                response_text = data["choices"][0]["message"]["content"]
                output_tokens = data.get("usage", {}).get("completion_tokens")
            except Exception as exc:
                print(f"ERROR: {exc}")
                break
            elapsed_ms = (time.monotonic() - t0) * 1000

            messages.append({"role": "assistant", "content": response_text})
            records.append({
                "turn": i,
                "user_msg": user_msg,
                "response": response_text,
                "output_tokens": output_tokens,
                "latency_ms": round(elapsed_ms, 1),
            })

            # Circuit breaker check
            if output_tokens is not None and output_tokens < CIRCUIT_BREAKER_MIN_TOKENS:
                low_token_streak += 1
            else:
                low_token_streak = 0

            print(
                f"{elapsed_ms / 1000:.1f}s  |  "
                f"out={output_tokens or '?'}t  |  "
                f"{user_msg[:60].replace(chr(10), ' ')}..."
            )

            if low_token_streak >= CIRCUIT_BREAKER_CONSECUTIVE:
                print(
                    f"\n  [CIRCUIT BREAKER] {low_token_streak} consecutive turns "
                    f"below {CIRCUIT_BREAKER_MIN_TOKENS}t — model appears stuck. "
                    f"Stopping at turn {i}/{len(turns_spec)}."
                )
                break

    return records


def fetch_trace(session_id: str) -> dict | None:
    """Fetch the full session trace from the proxy trace API."""
    try:
        r = httpx.get(f"{ADMIN_URL}/trace/sessions/{session_id}", timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"Trace API returned {r.status_code}", file=sys.stderr)
    except Exception as exc:
        print(f"Trace fetch failed: {exc}", file=sys.stderr)
    return None


# ── HTML Generation ───────────────────────────────────────────────────────────

def generate_html(
    scenario_name: str,
    session_id: str,
    scenario_path: str,
    turn_records: list[dict],
    trace: dict | None,
    output_path: Path,
) -> None:
    """Embed all data into a self-contained HTML file."""
    # Merge per-turn trace data into turn records by position.
    # Trace turn_number is 0-indexed; turn_records.turn is 1-indexed.
    merged_turns = [dict(r) for r in turn_records]
    if trace and trace.get("turns"):
        trace_turns = sorted(trace["turns"], key=lambda t: t.get("turn_number", 0))
        for i, rec in enumerate(merged_turns):
            if i < len(trace_turns):
                rec["trace"] = trace_turns[i]

    payload = {
        "scenario": scenario_name,
        "scenario_path": scenario_path,
        "session_id": session_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "turns": merged_turns,
        "trace": trace,  # Keep full session trace (summary + all turns) for header stats
    }
    data_json = json.dumps(payload, ensure_ascii=False, default=str)
    html = _HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    output_path.write_text(html, encoding="utf-8")
    print(f"\nExplorer saved -> {output_path}")
    print(f"Open in browser: file://{output_path.resolve()}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archolith Context Explorer — run a scenario and generate HTML report",
    )
    parser.add_argument("--turns", type=int, default=8, help="Turns to run (default: 8)")
    parser.add_argument(
        "--scenario",
        default="scripts/scenarios/taskflow.json",
        help="Scenario JSON file",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output HTML path (default: scripts/results/explorer_<ts>.html)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Override session ID (default: auto-generated)",
    )
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    if not scenario_path.exists():
        print(f"Scenario not found: {scenario_path}", file=sys.stderr)
        sys.exit(1)

    session_id = args.session_id or f"explorer-{uuid.uuid4().hex[:12]}"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    scenario_name = scenario.get("name", scenario_path.stem)

    # Run scenario
    turn_records = run_scenario(scenario_path, args.turns, session_id)

    # Wait briefly for the last turn's background trace-storage task to commit,
    # then retry up to 3 times until all expected turns are present.
    print("\nFetching trace", end="", flush=True)
    expected_turns = len(turn_records)
    trace = None
    for attempt in range(4):
        time.sleep(1.5)
        trace = fetch_trace(session_id)
        if trace and len(trace.get("turns", [])) >= expected_turns:
            break
        print(".", end="", flush=True)
    print(" ok" if trace else " failed (explorer will have partial data)")

    # Output path
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(args.output) if args.output else Path(f"scripts/results/explorer_{ts}.html")
    out.parent.mkdir(parents=True, exist_ok=True)

    generate_html(
        scenario_name=scenario_name,
        session_id=session_id,
        scenario_path=str(scenario_path),
        turn_records=turn_records,
        trace=trace,
        output_path=out,
    )


# ── HTML Template ─────────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Archolith Context Explorer</title>
<style>
:root {
  --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
  --border: #30363d; --text: #e6edf3; --text2: #8b949e; --text3: #6e7681;
  --green: #3fb950; --green-dim: #1a4429; --green-border: #2ea043;
  --blue: #58a6ff; --blue-dim: #0d2137; --blue-border: #1f6feb;
  --indigo: #a5b4fc; --indigo-dim: #1a1a3e; --indigo-border: #4c4fb5;
  --orange: #f97316; --orange-dim: #2d1a07; --orange-border: #c2531a;
  --gray: #484f58; --gray-dim: #161b22; --gray-border: #30363d;
  --purple: #bc8cff; --purple-dim: #21133a; --purple-border: #6e40c9;
  --red: #f85149; --yellow: #e3b341;
  --radius: 8px; --radius-sm: 4px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; line-height: 1.5; }
a { color: var(--blue); }
code, pre { font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 12px; }

/* Layout */
#app { max-width: 1200px; margin: 0 auto; padding: 24px 16px; }
.header { margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
.header h1 { font-size: 20px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
.header .meta { color: var(--text2); font-size: 12px; display: flex; gap: 16px; flex-wrap: wrap; }
.header .meta span { display: flex; align-items: center; gap: 4px; }

/* Stat pills */
.stat-row { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
.stat { background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 4px 10px; font-size: 12px; color: var(--text2); }
.stat strong { color: var(--text); }
.stat.green strong { color: var(--green); }
.stat.purple strong { color: var(--purple); }
.stat.orange strong { color: var(--orange); }

/* Token chart */
#chart-container { margin-bottom: 32px; }
#chart-container h2 { font-size: 13px; font-weight: 600; color: var(--text2); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
#token-chart { width: 100%; height: 120px; }

/* Turn cards */
.turn-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 16px; overflow: hidden;
}
.turn-header {
  display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  cursor: pointer; user-select: none;
  border-bottom: 1px solid transparent;
  transition: background 0.1s;
}
.turn-header:hover { background: var(--bg3); }
.turn-header.open { border-bottom-color: var(--border); }
.turn-num { font-size: 12px; font-weight: 700; color: var(--text2); min-width: 44px; }
.mode-badge {
  font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 20px;
  text-transform: uppercase; letter-spacing: 0.04em;
}
.mode-cold_start { background: var(--bg3); color: var(--text2); border: 1px solid var(--border); }
.mode-curator { background: var(--purple-dim); color: var(--purple); border: 1px solid var(--purple-border); }
.mode-graph { background: var(--blue-dim); color: var(--blue); border: 1px solid var(--blue-border); }
.mode-fallback { background: #2d0d0d; color: var(--red); border: 1px solid #5a1010; }
.mode-passthrough { background: var(--bg3); color: var(--text3); border: 1px solid var(--border); }
.turn-user-preview { flex: 1; color: var(--text2); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.turn-tokens { font-size: 11px; color: var(--text3); white-space: nowrap; margin-left: auto; }
.savings-chip {
  font-size: 11px; padding: 1px 6px; border-radius: 20px;
  background: var(--green-dim); color: var(--green); border: 1px solid var(--green-border);
  white-space: nowrap;
}
.savings-chip.zero { background: var(--bg3); color: var(--text3); border-color: var(--border); }
.toggle-arrow { color: var(--text3); font-size: 10px; transition: transform 0.15s; }
.toggle-arrow.open { transform: rotate(90deg); }

/* Turn body */
.turn-body { padding: 14px; display: none; }
.turn-body.open { display: block; }

/* Section headers within turn */
.section-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--text3); margin-bottom: 8px; margin-top: 16px;
}
.section-label:first-child { margin-top: 0; }

/* Context composition */
.context-comp { display: flex; flex-direction: column; gap: 8px; }
.context-row { display: flex; align-items: center; gap: 6px; }
.context-row-label { font-size: 11px; color: var(--text3); width: 70px; flex-shrink: 0; text-align: right; }
.context-blocks { display: flex; gap: 3px; flex-wrap: wrap; align-items: center; flex: 1; }
.ctx-block {
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 3px; font-size: 10px; font-weight: 500;
  padding: 3px 7px; white-space: nowrap; cursor: default;
  border: 1px solid transparent;
}
.ctx-block:hover { opacity: 0.85; }
.ctx-system { background: var(--green-dim); color: var(--green); border-color: var(--green-border); }
.ctx-retained { background: var(--blue-dim); color: var(--blue); border-color: var(--blue-border); }
.ctx-tail { background: var(--orange-dim); color: var(--orange); border-color: var(--orange-border); }
.ctx-dropped { background: var(--bg3); color: var(--text3); border-color: var(--border); text-decoration: line-through; opacity: 0.5; }
.ctx-user { background: var(--indigo-dim); color: var(--indigo); border-color: var(--indigo-border); }
.ctx-current { background: #0d2d1a; color: #3fb950; border-color: #2ea043; font-weight: 700; }
.ctx-arrow { color: var(--text3); font-size: 14px; }
.ctx-token-count { font-size: 11px; color: var(--text3); margin-left: auto; white-space: nowrap; }

/* Curator panel */
.curator-panel { background: var(--purple-dim); border: 1px solid var(--purple-border); border-radius: var(--radius-sm); padding: 10px 12px; margin-top: 4px; }
.curator-panel .curator-row { display: flex; gap: 8px; align-items: flex-start; margin-bottom: 6px; }
.curator-panel .curator-row:last-child { margin-bottom: 0; }
.curator-label { font-size: 11px; font-weight: 600; color: var(--purple); min-width: 120px; flex-shrink: 0; }
.curator-val { font-size: 12px; color: var(--text2); }
.retained-turn-pill {
  display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 20px; margin: 1px;
  background: var(--blue-dim); color: var(--blue); border: 1px solid var(--blue-border);
}
.dropped-turn-pill {
  display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 20px; margin: 1px;
  background: var(--bg3); color: var(--text3); border: 1px solid var(--border); text-decoration: line-through;
}
.ctx-block-text { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px; font-size: 11px; color: var(--text2); white-space: pre-wrap; margin-top: 6px; line-height: 1.6; }
.curator-expand-btn { cursor: pointer; font-size: 12px; color: var(--purple); background: none; border: 1px solid var(--purple-border); border-radius: var(--radius-sm); padding: 2px 10px; margin-top: 4px; display: inline-block; }
.curator-expand-btn:hover { background: var(--purple-dim); }
details.curator-details summary { list-style: none; }
details.curator-details summary::-webkit-details-marker { display: none; }
details.curator-details[open] .curator-expand-btn::after { content: " ▲"; }
details.curator-details:not([open]) .curator-expand-btn::after { content: " ▼"; }
.rewritten-msg { border-left: 3px solid var(--border); margin: 4px 0; padding: 6px 10px; font-size: 11px; }
.rewritten-msg.role-system { border-color: var(--green-border); background: var(--green-dim); }
.rewritten-msg.role-user { border-color: var(--blue-border); background: var(--blue-dim); }
.rewritten-msg.role-assistant { border-color: var(--indigo-border); background: var(--indigo-dim); }
.rewritten-msg .msg-role { font-weight: 700; font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; color: var(--text2); }
.rewritten-msg .msg-content { color: var(--text); white-space: pre-wrap; }

/* Facts strip */
.facts-strip { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
.fact-chip { background: var(--green-dim); color: var(--green); border: 1px solid var(--green-border); border-radius: 20px; font-size: 11px; padding: 2px 8px; }

/* Response section */
.response-toggle { cursor: pointer; user-select: none; display: flex; align-items: center; gap: 6px; color: var(--text2); font-size: 12px; }
.response-toggle:hover { color: var(--text); }
.response-body { display: none; background: var(--bg3); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px; margin-top: 8px; max-height: 400px; overflow-y: auto; }
.response-body.open { display: block; }
.response-text { white-space: pre-wrap; font-size: 12px; color: var(--text2); }

/* Metrics row */
.metrics-row { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 10px; font-size: 12px; color: var(--text3); }
.metrics-row span { display: flex; align-items: center; gap: 4px; }
.metrics-row .v { color: var(--text2); font-weight: 500; }
.metrics-row .v.good { color: var(--green); }
.metrics-row .v.warn { color: var(--yellow); }

/* Legend */
.legend { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; font-size: 12px; color: var(--text2); }
.legend-item { display: flex; align-items: center; gap: 5px; }
.legend-dot { width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }
</style>
</head>
<body>
<div id="app">
  <div id="header-slot"></div>
  <div id="chart-container">
    <h2>Token Usage Per Turn</h2>
    <svg id="token-chart"></svg>
  </div>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#3fb950"></div>Curator Facts (system)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#58a6ff"></div>Retained middle turn</div>
    <div class="legend-item"><div class="legend-dot" style="background:#a5b4fc"></div>User/asst message</div>
    <div class="legend-item"><div class="legend-dot" style="background:#f97316"></div>Coherence tail (always kept)</div>
    <div class="legend-item"><div class="legend-dot" style="background:#484f58"></div>Dropped by curator</div>
  </div>
  <div id="turns-slot"></div>
</div>

<script>
const DATA = __DATA_JSON__;

// ── Helpers ─────────────────────────────────────────────────────────────────

function esc(s) {
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function fmt(n, unit='') {
  if (n == null) return '?';
  if (n >= 1000) return (n/1000).toFixed(1) + 'k' + unit;
  return n + unit;
}

function traceByTurn(n) {
  if (!DATA.trace || !DATA.trace.turns) return null;
  return DATA.trace.turns.find(t => t.turn_number === n) || null;
}

function modeBadge(mode) {
  return `<span class="mode-badge mode-${esc(mode)}">${esc(mode || 'passthrough')}</span>`;
}

// Group messages into segments: system, middle turns (each user+asst run), tail
// Returns array of segment objects.
function segmentMessages(msgs, tailSize = 3) {
  if (!msgs || !msgs.length) return [];
  const segments = [];
  let i = 0;

  // Leading system message(s)
  while (i < msgs.length && msgs[i].role === 'system') {
    const content = typeof msgs[i].content === 'string' ? msgs[i].content : JSON.stringify(msgs[i].content);
    segments.push({ type: 'system', content, index: i });
    i++;
  }

  // Count user messages to determine tail boundary
  const nonSystem = msgs.slice(i);
  let userCount = 0;
  for (const m of nonSystem) { if (m.role === 'user') userCount++; }
  const tailUserStart = Math.max(1, userCount - tailSize + 1);

  let userIdx = 0;
  let j = 0;
  while (j < nonSystem.length) {
    const m = nonSystem[j];
    if (m.role === 'user') {
      userIdx++;
      const isTail = userIdx >= tailUserStart;
      // Collect this turn (user + any following non-user messages until next user)
      const turnMsgs = [m];
      j++;
      while (j < nonSystem.length && nonSystem[j].role !== 'user') {
        turnMsgs.push(nonSystem[j]);
        j++;
      }
      const content = m.content ? (typeof m.content === 'string' ? m.content : JSON.stringify(m.content)) : '';
      segments.push({
        type: isTail ? 'tail' : 'middle',
        turnNum: userIdx,
        isTail,
        msgs: turnMsgs,
        userPreview: content.slice(0, 60).replace(/\n/g, ' '),
      });
    } else {
      j++;
    }
  }

  return segments;
}

function ctxBlock(cls, label, title='') {
  return `<span class="ctx-block ${cls}" title="${esc(title)}">${esc(label)}</span>`;
}

function renderContextRow(label, msgs, retainedSet, allTurns, isCurrent) {
  const segs = segmentMessages(msgs, 3);
  if (!segs.length) return '';

  let blocks = '';
  // Compute approximate total chars for proportional sizing hint
  let totalChars = 0;
  for (const s of segs) {
    if (s.type === 'system') totalChars += (s.content || '').length;
    else for (const m of (s.msgs || [])) totalChars += ((typeof m.content === 'string' ? m.content : '') || '').length;
  }

  for (const seg of segs) {
    if (seg.type === 'system') {
      blocks += ctxBlock('ctx-system', 'Curator Facts', seg.content.slice(0, 200));
    } else if (seg.type === 'tail') {
      const isCurrentTurn = seg.turnNum === allTurns;
      blocks += ctxBlock(isCurrentTurn ? 'ctx-current' : 'ctx-tail', `t${seg.turnNum} ${isCurrentTurn ? '(now)' : 'tail'}`, seg.userPreview);
    } else {
      // middle
      const isDropped = retainedSet !== null && !retainedSet.has(seg.turnNum);
      blocks += ctxBlock(isDropped ? 'ctx-dropped' : 'ctx-retained',
        `t${seg.turnNum}${isDropped ? ' ✗' : ''}`,
        seg.userPreview + (isDropped ? ' [dropped by curator]' : ''));
    }
  }

  // Token count
  const tokenLabel = label === 'proxy' ? '' : '';
  return `
    <div class="context-row">
      <span class="context-row-label">${esc(label)}</span>
      <div class="context-blocks">${blocks}</div>
    </div>`;
}

// ── Header ───────────────────────────────────────────────────────────────────

function renderHeader() {
  const tr = DATA.trace;
  const s = tr && tr.summary ? tr.summary : {};
  const turns = DATA.turns || [];
  const totalOrigTok = (tr && tr.turns ? tr.turns.reduce((a,t) => a + (t.input_tokens||0), 0) : 0);
  const totalRewrTok = (tr && tr.turns ? tr.turns.reduce((a,t) => a + (t.rewritten_tokens||0), 0) : 0);
  const totalSavings = Math.max(0, totalOrigTok - totalRewrTok);
  const savingsPct = totalOrigTok > 0 ? (totalSavings / totalOrigTok * 100).toFixed(1) : 0;
  const curatorTurns = tr && tr.turns ? tr.turns.filter(t => t.assembly_mode === 'curator').length : 0;
  const factCount = s.total_facts_stored || 0;

  return `
<div class="header">
  <h1>Archolith Context Explorer</h1>
  <div class="meta">
    <span>Scenario: <strong>${esc(DATA.scenario)}</strong></span>
    <span>Session: <code>${esc(DATA.session_id)}</code></span>
    <span>Model: <strong>${esc(DATA.model)}</strong></span>
    <span>${esc(DATA.generated_at.slice(0,19).replace('T',' '))} UTC</span>
  </div>
  <div class="stat-row">
    <div class="stat"><strong>${turns.length}</strong> turns</div>
    <div class="stat purple"><strong>${curatorTurns}</strong> curator</div>
    <div class="stat"><strong>${fmt(totalOrigTok,'t')}</strong> original tokens</div>
    <div class="stat ${totalSavings > 0 ? 'green' : ''}"><strong>${fmt(totalSavings,'t')} (${savingsPct}%)</strong> saved</div>
    <div class="stat orange"><strong>${factCount}</strong> facts stored</div>
  </div>
</div>`;
}

// ── Token Chart (SVG) ─────────────────────────────────────────────────────────

function renderChart() {
  const svgEl = document.getElementById('token-chart');
  const tr = DATA.trace;
  if (!tr || !tr.turns || !tr.turns.length) {
    svgEl.innerHTML = '<text x="10" y="20" fill="#6e7681" font-size="12">No trace data</text>';
    return;
  }
  const traceTurns = tr.turns.sort((a,b) => a.turn_number - b.turn_number);
  const W = svgEl.parentElement.clientWidth || 800;
  const H = 120;
  const PAD = { top: 10, right: 20, bottom: 30, left: 50 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  svgEl.setAttribute('width', W);
  svgEl.setAttribute('height', H);

  const origVals = traceTurns.map(t => t.input_tokens || 0);
  const rewrVals = traceTurns.map(t => t.rewritten_tokens || 0);
  const maxV = Math.max(...origVals, ...rewrVals, 1);
  const n = traceTurns.length;

  function px(i) { return PAD.left + (i / Math.max(n-1,1)) * plotW; }
  function py(v) { return PAD.top + plotH - (v / maxV) * plotH; }

  function polyline(vals, stroke) {
    const pts = vals.map((v,i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(' ');
    return `<polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/>`;
  }

  function dots(vals, fill, data) {
    return vals.map((v,i) => {
      const t = data[i];
      const tip = `Turn ${t.turn_number}: ${v}t (${t.assembly_mode})`;
      return `<circle cx="${px(i).toFixed(1)}" cy="${py(v).toFixed(1)}" r="3.5" fill="${fill}" title="${esc(tip)}"/>`;
    }).join('');
  }

  // Y axis labels
  const yLabels = [0, Math.round(maxV/2), maxV].map(v =>
    `<text x="${PAD.left - 4}" y="${py(v) + 4}" fill="#6e7681" font-size="11" text-anchor="end">${fmt(v)}</text>`
  ).join('');

  // X axis turn labels
  const step = n > 12 ? Math.ceil(n/8) : 1;
  const xLabels = traceTurns.filter((_,i) => i % step === 0).map((t,_,arr) => {
    const i = traceTurns.indexOf(t);
    return `<text x="${px(i).toFixed(1)}" y="${H - 6}" fill="#6e7681" font-size="11" text-anchor="middle">t${t.turn_number}</text>`;
  }).join('');

  // Savings fill between curves
  const fillPts = origVals.map((v,i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(' ') + ' ' +
    rewrVals.map((v,i) => `${px(n-1-i).toFixed(1)},${py(rewrVals[n-1-i]).toFixed(1)}`).join(' ');

  svgEl.innerHTML = `
    <polygon points="${fillPts}" fill="#1a4429" opacity="0.5"/>
    ${polyline(origVals, '#6e7681')}
    ${polyline(rewrVals, '#3fb950')}
    ${dots(origVals, '#6e7681', traceTurns)}
    ${dots(rewrVals, '#3fb950', traceTurns)}
    ${yLabels}${xLabels}
    <text x="${PAD.left+8}" y="${PAD.top+14}" fill="#6e7681" font-size="11">original</text>
    <text x="${PAD.left+8}" y="${PAD.top+28}" fill="#3fb950" font-size="11">proxy (rewritten)</text>
  `;
}

// ── Turn Card ─────────────────────────────────────────────────────────────────

function renderTurnCard(turnRecord, idx) {
  const n = turnRecord.turn;
  // Use pre-merged trace (by position) from the turn record;
  // fall back to lookup by turn_number for backward compatibility.
  const tr = turnRecord.trace || traceByTurn(n - 1) || null;
  const mode = tr ? tr.assembly_mode : 'unknown';
  const origTok = tr ? tr.input_tokens : 0;
  const rewrTok = tr ? (tr.rewritten_tokens || origTok) : 0;
  const savings = Math.max(0, origTok - rewrTok);
  const savingsPct = origTok > 0 ? (savings / origTok * 100).toFixed(1) : 0;
  const factsStored = tr ? tr.facts_stored : 0;
  const latencyMs = turnRecord.latency_ms || (tr ? tr.upstream_latency_ms : 0);
  const asmLatency = tr ? tr.assembly_latency_ms : 0;

  const origMsgs = tr ? tr.original_messages : null;
  const rewrMsgs = tr ? tr.rewritten_messages : null;

  // Determine how many turns are in context so far (total user messages before this turn)
  const totalUserTurns = n; // turn N is the Nth user message
  const retainedTurns = tr ? tr.curator_retained_turns : null;
  const retainedSet = retainedTurns !== null ? new Set(retainedTurns) : null;

  // Context composition
  let ctxHtml = '';
  if (origMsgs && origMsgs.length) {
    ctxHtml += `<div class="section-label">Context Composition</div>`;
    ctxHtml += `<div class="context-comp">`;
    ctxHtml += renderContextRow('original', origMsgs, null, totalUserTurns, false);
    if (rewrMsgs && rewrMsgs.length && mode !== 'cold_start' && mode !== 'passthrough') {
      ctxHtml += renderContextRow('proxy', rewrMsgs, retainedSet, totalUserTurns, false);
    }
    ctxHtml += `</div>`;
  }

  // Curator panel
  let curatorHtml = '';
  if (mode === 'curator' && tr) {
    const retainedList = retainedTurns || [];
    // Figure out which turns were in the middle (not tail, not current)
    const middleMax = Math.max(0, totalUserTurns - 4); // rough estimate of tail boundary
    const allMiddleTurns = Array.from({length: middleMax}, (_,i) => i+1);
    const droppedTurns = allMiddleTurns.filter(t => !retainedList.includes(t));

    const retainedPills = retainedList.length > 0
      ? retainedList.map(t => `<span class="retained-turn-pill">t${t}</span>`).join('')
      : '<span style="color:var(--text3);font-size:12px">none selected</span>';

    const droppedPills = droppedTurns.length > 0
      ? droppedTurns.map(t => `<span class="dropped-turn-pill">t${t}</span>`).join('')
      : '<span style="color:var(--text3);font-size:12px">none</span>';

    curatorHtml = `
<div class="section-label">Curator</div>
<div class="curator-panel">
  <div class="curator-row">
    <span class="curator-label">Retained turns</span>
    <span class="curator-val">${retainedList.length > 0 ? retainedPills : '<em style="color:var(--text3)">none (curator did not call select_relevant_turns)</em>'}</span>
  </div>`;

    if (droppedTurns.length > 0) {
      curatorHtml += `
  <div class="curator-row">
    <span class="curator-label">Dropped turns</span>
    <span class="curator-val">${droppedPills}</span>
  </div>`;
    }

    if (tr.curator_context_block) {
      const ctxId = `curator-ctx-${n}`;
      curatorHtml += `
  <div class="curator-row" style="flex-direction:column;align-items:flex-start">
    <details class="curator-details" style="width:100%">
      <summary><span class="curator-expand-btn">Curator context block (${tr.curator_context_block.length} chars)</span></summary>
      <pre class="ctx-block-text">${esc(tr.curator_context_block)}</pre>
    </details>
  </div>`;
    }

    // Rewritten prompt — full message array sent upstream
    if (rewrMsgs && rewrMsgs.length) {
      const msgRows = rewrMsgs.map(msg => {
        const role = msg.role || 'unknown';
        let content = '';
        if (typeof msg.content === 'string') {
          content = msg.content;
        } else if (Array.isArray(msg.content)) {
          content = msg.content.map(p => p.text || '').join('');
        }
        return `<div class="rewritten-msg role-${esc(role)}"><div class="msg-role">${esc(role)}</div><div class="msg-content">${esc(content)}</div></div>`;
      }).join('');
      curatorHtml += `
  <div class="curator-row" style="flex-direction:column;align-items:flex-start">
    <details class="curator-details" style="width:100%">
      <summary><span class="curator-expand-btn">Rewritten prompt (${rewrMsgs.length} messages, ${rewrTok}t)</span></summary>
      <div style="margin-top:6px">${msgRows}</div>
    </details>
  </div>`;
    }

    curatorHtml += `</div>`;
  }

  // Facts chips
  const extractedFacts = (tr && tr.extracted_facts) ? tr.extracted_facts.slice(0, 6) : [];
  let factsHtml = '';
  if (factsStored > 0) {
    factsHtml = `<div class="section-label">Facts Extracted (${factsStored})</div><div class="facts-strip">`;
    for (const f of extractedFacts) {
      const content = (f.content || '').slice(0, 80);
      factsHtml += `<span class="fact-chip" title="${esc(f.content || '')}">${esc(content)}${content.length < (f.content||'').length ? '…' : ''}</span>`;
    }
    if (factsStored > extractedFacts.length) {
      factsHtml += `<span class="fact-chip" style="background:var(--bg3);color:var(--text3)">+${factsStored - extractedFacts.length} more</span>`;
    }
    factsHtml += `</div>`;
  }

  // Response
  const responseText = turnRecord.response || '';
  const outTok = turnRecord.output_tokens || tr && tr.output_tokens;
  const respId = `resp-${n}`;
  const responseHtml = `
<div class="section-label">
  <span class="response-toggle" onclick="toggleResp('${respId}')">
    <span id="${respId}-arrow" class="toggle-arrow">&#9658;</span>
    Response  <span style="color:var(--text3)">${outTok ? outTok+'t' : ''} ${(latencyMs/1000).toFixed(1)}s</span>
  </span>
</div>
<div id="${respId}" class="response-body">
  <pre class="response-text">${esc(responseText)}</pre>
</div>`;

  // Metrics
  const savingsChipCls = savings > 0 ? '' : 'zero';
  const metricsHtml = `
<div class="metrics-row">
  <span>in: <span class="v">${fmt(origTok)}t</span></span>
  <span>rewritten: <span class="v ${savings>0?'good':''}">${fmt(rewrTok)}t</span></span>
  <span>savings: <span class="v ${savings>0?'good':'warn'}">${fmt(savings)}t (${savingsPct}%)</span></span>
  ${factsStored > 0 ? `<span>facts: <span class="v good">+${factsStored}</span></span>` : ''}
  <span>asm: <span class="v">${(asmLatency/1000).toFixed(1)}s</span></span>
  <span>total: <span class="v">${(latencyMs/1000).toFixed(1)}s</span></span>
</div>`;

  const cardId = `card-${n}`;
  const bodyId = `body-${n}`;
  const openByDefault = n <= 3 || mode === 'curator';

  return `
<div class="turn-card" id="${cardId}">
  <div class="turn-header ${openByDefault ? 'open' : ''}" onclick="toggleCard('${bodyId}', this)">
    <span class="turn-num">Turn ${n}</span>
    ${modeBadge(mode)}
    <span class="turn-user-preview">${esc((turnRecord.user_msg || '').slice(0,120).replace(/\n/g,' '))}</span>
    <span class="savings-chip ${savingsChipCls}">${fmt(savings)}t saved</span>
    <span class="turn-tokens">${fmt(origTok)}t &rarr; ${fmt(rewrTok)}t</span>
    <span class="toggle-arrow ${openByDefault ? 'open' : ''}">&#9658;</span>
  </div>
  <div id="${bodyId}" class="turn-body ${openByDefault ? 'open' : ''}">
    ${ctxHtml}
    ${curatorHtml}
    ${factsHtml}
    ${responseHtml}
    ${metricsHtml}
  </div>
</div>`;
}

// ── Toggle helpers ────────────────────────────────────────────────────────────

function toggleCard(bodyId, headerEl) {
  const body = document.getElementById(bodyId);
  const arrow = headerEl.querySelector('.toggle-arrow');
  if (body.classList.contains('open')) {
    body.classList.remove('open');
    headerEl.classList.remove('open');
    if (arrow) { arrow.classList.remove('open'); }
  } else {
    body.classList.add('open');
    headerEl.classList.add('open');
    if (arrow) { arrow.classList.add('open'); }
  }
}

function toggleResp(id) {
  const el = document.getElementById(id);
  const arrow = document.getElementById(id + '-arrow');
  if (el.classList.contains('open')) {
    el.classList.remove('open');
    if (arrow) arrow.classList.remove('open');
  } else {
    el.classList.add('open');
    if (arrow) arrow.classList.add('open');
  }
}

// ── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('header-slot').innerHTML = renderHeader();
  renderChart();

  const turns = DATA.turns || [];
  const container = document.getElementById('turns-slot');
  container.innerHTML = turns.map((t, i) => renderTurnCard(t, i)).join('');
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
