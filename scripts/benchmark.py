"""Archolith benchmark suite — compare proxy-rewritten vs raw passthrough.

Sends the same multi-turn conversation through:
  1. Direct upstream API (baseline — full conversation history every turn)
  2. Archolith proxy (test — assembly/rewriting may compress the middle)

After each turn, fetches the proxy trace to capture what the engine did
(assembly mode, facts injected, token savings). Outputs a per-turn
comparison table and JSON results.

Supports external scenario files (JSON), configurable token budgets,
fact-probe quality testing, and organized output directories.

Usage:
    python scripts/benchmark.py --scenario scenarios/taskflow.json
    python scripts/benchmark.py --scenario scenarios/taskflow.json --budget 4000
    python scripts/benchmark.py --all --budgets 4000,8000,15000,32000
    python scripts/benchmark.py --scenario scenarios/ruler_recall.json --probes-only

Requires UPSTREAM_API_KEY in .env or env vars. The proxy must be running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:9800/v1")
DIRECT_URL = os.getenv("UPSTREAM_BASE_URL", "https://integrate.api.nvidia.com/v1")
API_KEY = os.getenv("UPSTREAM_API_KEY", "")
MODEL = os.getenv("BENCHMARK_MODEL", "gpt-4o-mini")

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------

@dataclass
class FactProbe:
    after_turn: int
    question: str
    expected_keywords: list[str]

@dataclass
class Scenario:
    name: str
    description: str
    system_prompt: str
    turns: list[str]
    fact_probes: list[FactProbe] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "Scenario":
        with open(path) as f:
            data = json.load(f)
        probes = [FactProbe(**p) for p in data.get("fact_probes", [])]
        return cls(
            name=data["name"],
            description=data["description"],
            system_prompt=data["system_prompt"],
            turns=data["turns"],
            fact_probes=probes,
        )


def list_scenarios() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def send_chat(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    messages: list[dict],
    model: str,
    max_retries: int = 5,
) -> tuple[str, float, dict]:
    """Send a chat completion request. Returns (response_text, latency_ms, usage_dict).

    Retries on 429 (rate limit) with exponential backoff. Parses Retry-After
    header when available.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.3,
    }

    total_start = time.monotonic()
    for attempt in range(max_retries + 1):
        start = time.monotonic()
        try:
            resp = client.post(url, json=body, headers=headers, timeout=300)
        except httpx.TimeoutException:
            latency_ms = (time.monotonic() - total_start) * 1000
            return f"[TIMEOUT after {latency_ms/1000:.0f}s]", latency_ms, {}
        except httpx.HTTPError as e:
            latency_ms = (time.monotonic() - total_start) * 1000
            return f"[HTTP ERROR]: {e}", latency_ms, {}

        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if retry_after:
                try:
                    wait = int(retry_after)
                except ValueError:
                    wait = 2 ** attempt * 10
            else:
                wait = 2 ** attempt * 10
            wait = min(wait, 300)
            print(f"  [429] Rate limited, waiting {wait}s before retry {attempt + 1}/{max_retries}...")
            time.sleep(wait)
            continue

        break

    latency_ms = (time.monotonic() - total_start) * 1000

    if resp.status_code != 200:
        return f"[ERROR {resp.status_code}]: {resp.text[:300]}", latency_ms, {}

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    usage = data.get("usage", {})
    return text, latency_ms, usage


def _proxy_base(proxy_url: str) -> str:
    return proxy_url.rstrip("/").removesuffix("/v1")


def get_proxy_trace(client: httpx.Client, proxy_url: str, session_id: str | None = None) -> dict:
    base = _proxy_base(proxy_url)
    try:
        if not session_id:
            resp = client.get(f"{base}/trace/sessions", timeout=10)
            if resp.status_code != 200:
                return {"error": f"trace sessions {resp.status_code}"}
            sessions = resp.json().get("sessions", [])
            if not sessions:
                return {"error": "no trace sessions"}
            session_id = sessions[0]["session_id"]

        resp2 = client.get(f"{base}/trace/sessions/{session_id}", timeout=10)
        if resp2.status_code != 200:
            return {"error": f"trace session detail {resp2.status_code}"}
        return resp2.json()
    except Exception as e:
        return {"error": str(e)}


def set_proxy_budget(client: httpx.Client, proxy_url: str, budget: int) -> bool:
    """Set the proxy's context token budget via admin API. Returns True if successful."""
    base = _proxy_base(proxy_url)
    try:
        resp = client.post(
            f"{base}/admin/config",
            json={"context_token_budget": budget},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 1
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            total += estimate_tokens(c)
        elif isinstance(c, list):
            for part in c:
                total += estimate_tokens(part.get("text", ""))
    return max(1, total)


# ---------------------------------------------------------------------------
# Fact probe evaluation
# ---------------------------------------------------------------------------

def run_fact_probes(
    client: httpx.Client,
    scenario: Scenario,
    direct_history: list[dict],
    proxy_history: list[dict],
    proxy_url: str,
    direct_url: str,
    model: str,
    current_turn: int,
    api_key: str = "",
) -> list[dict]:
    """Run any fact probes scheduled after the current turn."""
    _key = api_key or API_KEY
    results = []
    for probe in scenario.fact_probes:
        if probe.after_turn != current_turn:
            continue

        probe_msg = {"role": "user", "content": probe.question}

        # Direct (baseline)
        direct_messages = direct_history + [probe_msg]
        direct_text, _, _ = send_chat(client, direct_url, _key, direct_messages, model)

        # Proxy
        proxy_messages = proxy_history + [probe_msg]
        proxy_text, _, _ = send_chat(client, proxy_url, _key, proxy_messages, model)

        # Score: what fraction of expected keywords appear in the response
        direct_hits = sum(1 for kw in probe.expected_keywords if kw.lower() in direct_text.lower())
        proxy_hits = sum(1 for kw in probe.expected_keywords if kw.lower() in proxy_text.lower())
        total_kw = len(probe.expected_keywords)

        result = {
            "after_turn": probe.after_turn,
            "question": probe.question,
            "expected_keywords": probe.expected_keywords,
            "direct_recall": round(direct_hits / total_kw, 3) if total_kw else 0,
            "proxy_recall": round(proxy_hits / total_kw, 3) if total_kw else 0,
            "direct_hits": direct_hits,
            "proxy_hits": proxy_hits,
            "total_keywords": total_kw,
            "direct_response_preview": direct_text[:200],
            "proxy_response_preview": proxy_text[:200],
        }
        results.append(result)

        status = "PASS" if proxy_hits >= direct_hits else "DEGRADED"
        print(f"  [probe]  After turn {current_turn}: {status} — "
              f"proxy {proxy_hits}/{total_kw} vs direct {direct_hits}/{total_kw} keywords recalled")

    return results


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def _checkpoint_path(output_dir: Path, scenario_name: str, budget: int | None) -> Path:
    budget_str = f"_{budget}b" if budget else ""
    return output_dir / f".checkpoint_{scenario_name}{budget_str}.json"


def _save_checkpoint(
    path: Path, scenario_name: str, budget: int | None,
    results: list, probe_results: list,
    direct_history: list, proxy_history: list,
    proxy_session_id: str | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "scenario": scenario_name, "budget": budget,
            "results": results, "probe_results": probe_results,
            "direct_history": direct_history, "proxy_history": proxy_history,
            "proxy_session_id": proxy_session_id,
        }, f)


def _load_checkpoint(path: Path) -> dict | None:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def run_benchmark(
    scenario: Scenario,
    max_turns: int | None,
    proxy_url: str,
    direct_url: str,
    model: str,
    budget: int | None,
    output_dir: Path = Path("scripts/results"),
    resume: bool = False,
    api_key: str = "",
) -> dict:
    """Run the benchmark for a single scenario/budget combination."""
    _key = api_key or API_KEY
    results = []
    probe_results = []
    direct_history: list[dict] = []
    proxy_history: list[dict] = []
    proxy_session_id: str | None = None
    start_turn = 0

    ckpt_path = _checkpoint_path(output_dir, scenario.name, budget)

    if resume:
        ckpt = _load_checkpoint(ckpt_path)
        if ckpt and ckpt["scenario"] == scenario.name and ckpt["budget"] == budget:
            results = ckpt["results"]
            probe_results = ckpt["probe_results"]
            direct_history = ckpt["direct_history"]
            proxy_history = ckpt["proxy_history"]
            proxy_session_id = ckpt["proxy_session_id"]
            start_turn = len(results)
            print(f"  Resuming from turn {start_turn + 1} (checkpoint has {start_turn} turns)")

    if not direct_history:
        system_msg = {"role": "system", "content": scenario.system_prompt}
        direct_history.append(system_msg)
        proxy_history.append(system_msg)

    turns = scenario.turns[:max_turns] if max_turns else scenario.turns

    with httpx.Client() as client:
        # Set budget if requested
        if budget:
            if set_proxy_budget(client, proxy_url, budget):
                print(f"  Budget set to {budget} tokens")
            else:
                print(f"  WARNING: Could not set budget via admin API, using proxy default")

        for i, user_msg in enumerate(turns, 1):
            if i <= start_turn:
                continue
            print(f"\n{'='*60}")
            print(f"  TURN {i}/{len(turns)}: {scenario.name}")
            print(f"  User: {user_msg[:80]}...")
            print(f"{'='*60}")

            direct_history.append({"role": "user", "content": user_msg})
            proxy_history.append({"role": "user", "content": user_msg})

            direct_est_tokens = estimate_messages_tokens(direct_history)
            proxy_est_tokens = estimate_messages_tokens(proxy_history)

            # --- Direct call ---
            print(f"  [direct] Sending {len(direct_history)} messages (~{direct_est_tokens} tokens)...")
            direct_text, direct_latency, direct_usage = send_chat(
                client, direct_url, _key, direct_history, model
            )
            direct_input = direct_usage.get("prompt_tokens", direct_est_tokens)
            direct_output = direct_usage.get("completion_tokens", estimate_tokens(direct_text))
            print(f"  [direct] {direct_input} in / {direct_output} out in {direct_latency:.0f}ms")

            # --- Proxy call ---
            print(f"  [proxy]  Sending {len(proxy_history)} messages (~{proxy_est_tokens} tokens)...")
            proxy_text, proxy_latency, proxy_usage = send_chat(
                client, proxy_url, _key, proxy_history, model
            )
            proxy_input = proxy_usage.get("prompt_tokens", proxy_est_tokens)
            proxy_output = proxy_usage.get("completion_tokens", estimate_tokens(proxy_text))
            print(f"  [proxy]  {proxy_input} in / {proxy_output} out in {proxy_latency:.0f}ms")

            # --- Fetch proxy trace ---
            time.sleep(3)
            trace = get_proxy_trace(client, proxy_url, session_id=proxy_session_id)
            trace_turns = trace.get("turns", [])

            expected_turn = i - 1
            this_trace = {}
            for t in reversed(trace_turns):
                if t.get("turn_number") == expected_turn:
                    this_trace = t
                    break
            if not this_trace and trace_turns:
                this_trace = trace_turns[-1]

            if not proxy_session_id and trace.get("summary", {}).get("session_id"):
                proxy_session_id = trace["summary"]["session_id"]
                print(f"  [trace]  session_id={proxy_session_id}")

            assembly_mode = this_trace.get("assembly_mode", "unknown")
            savings_tokens = this_trace.get("savings_tokens", 0)
            savings_ratio = this_trace.get("savings_ratio", 0.0)
            rewritten_tokens = this_trace.get("rewritten_tokens", 0)
            facts_stored = this_trace.get("facts_stored", 0)
            assembly_latency = this_trace.get("assembly_latency_ms", 0.0)
            extraction_latency = this_trace.get("extraction_latency_ms", 0.0)
            trace_input_tokens = this_trace.get("input_tokens", 0)

            print(f"  [trace]  assembly={assembly_mode}, "
                  f"input={trace_input_tokens}, rewritten={rewritten_tokens}, "
                  f"savings={savings_tokens} ({savings_ratio:.1%}), "
                  f"facts_stored={facts_stored}")

            direct_history.append({"role": "assistant", "content": direct_text})
            proxy_history.append({"role": "assistant", "content": proxy_text})

            result = {
                "turn": i,
                "user_msg_preview": user_msg[:80],
                "user_msg": user_msg,
                "direct": {
                    "input_tokens": direct_input,
                    "output_tokens": direct_output,
                    "latency_ms": round(direct_latency, 1),
                    "response_preview": direct_text[:150] if direct_text else "",
                    "response": direct_text,
                },
                "proxy": {
                    "input_tokens": proxy_input,
                    "output_tokens": proxy_output,
                    "latency_ms": round(proxy_latency, 1),
                    "response_preview": proxy_text[:150] if proxy_text else "",
                    "response": proxy_text,
                },
                "trace": {
                    "assembly_mode": assembly_mode,
                    "input_tokens": trace_input_tokens,
                    "rewritten_tokens": rewritten_tokens,
                    "savings_tokens": savings_tokens,
                    "savings_ratio": round(savings_ratio, 4),
                    "facts_stored": facts_stored,
                    "assembly_latency_ms": round(assembly_latency, 1),
                    "extraction_latency_ms": round(extraction_latency, 1),
                    "session_id": proxy_session_id or "",
                },
            }
            results.append(result)

            # Run fact probes after this turn
            probes = run_fact_probes(
                client, scenario, direct_history, proxy_history,
                proxy_url, direct_url, model, i, api_key=_key,
            )
            probe_results.extend(probes)

            # Checkpoint after each turn for resume support
            _save_checkpoint(
                ckpt_path, scenario.name, budget,
                results, probe_results,
                direct_history, proxy_history, proxy_session_id,
            )

    # Clean up checkpoint on successful completion
    if ckpt_path.exists():
        ckpt_path.unlink()

    # Compute summary
    total_direct_input = sum(r["direct"]["input_tokens"] for r in results)
    total_proxy_input = sum(r["proxy"]["input_tokens"] for r in results)
    total_savings = sum(r["trace"]["savings_tokens"] for r in results)

    probe_summary = {}
    if probe_results:
        avg_direct_recall = sum(p["direct_recall"] for p in probe_results) / len(probe_results)
        avg_proxy_recall = sum(p["proxy_recall"] for p in probe_results) / len(probe_results)
        probe_summary = {
            "total_probes": len(probe_results),
            "avg_direct_recall": round(avg_direct_recall, 3),
            "avg_proxy_recall": round(avg_proxy_recall, 3),
            "recall_preservation": round(avg_proxy_recall / avg_direct_recall, 3) if avg_direct_recall > 0 else 0,
        }

    return {
        "scenario": scenario.name,
        "description": scenario.description,
        "model": model,
        "budget": budget,
        "turns_run": len(results),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "total_direct_input_tokens": total_direct_input,
            "total_proxy_input_tokens": total_proxy_input,
            "total_savings_tokens": total_savings,
            "overall_savings_ratio": round(total_savings / total_direct_input, 4) if total_direct_input else 0,
        },
        "quality": probe_summary,
        "turns": results,
        "fact_probes": probe_results,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_summary(data: dict) -> None:
    results = data["turns"]
    print(f"\n{'='*100}")
    print(f"  BENCHMARK SUMMARY: {data['scenario']} (budget={data['budget'] or 'default'})")
    print(f"{'='*100}")

    header = (
        f"{'Turn':>4}  {'Direct In':>10}  {'Proxy In':>10}  {'Trace In':>10}  "
        f"{'Rewritten':>10}  {'Savings':>14}  {'Assembly':>14}  {'Facts':>5}  "
        f"{'D ms':>7}  {'P ms':>7}"
    )
    print(header)
    print("-" * 110)

    for r in results:
        d = r["direct"]
        p = r["proxy"]
        t = r["trace"]
        savings_str = f"{t['savings_tokens']:>5} ({t['savings_ratio']:.0%})"
        print(
            f"{r['turn']:>4}  "
            f"{d['input_tokens']:>10}  "
            f"{p['input_tokens']:>10}  "
            f"{t['input_tokens']:>10}  "
            f"{t['rewritten_tokens']:>10}  "
            f"{savings_str:>14}  "
            f"{t['assembly_mode']:>14}  "
            f"{t['facts_stored']:>5}  "
            f"{d['latency_ms']:>7.0f}  "
            f"{p['latency_ms']:>7.0f}"
        )

    s = data["summary"]
    print("-" * 110)
    print(f"  Total direct input tokens: {s['total_direct_input_tokens']:,}")
    print(f"  Total proxy input tokens:  {s['total_proxy_input_tokens']:,}")
    print(f"  Total savings:             {s['total_savings_tokens']:,}")
    print(f"  Overall savings ratio:     {s['overall_savings_ratio']:.1%}")

    if data.get("quality"):
        q = data["quality"]
        print(f"\n  Fact Probe Quality:")
        print(f"    Probes run:              {q['total_probes']}")
        print(f"    Avg direct recall:       {q['avg_direct_recall']:.1%}")
        print(f"    Avg proxy recall:        {q['avg_proxy_recall']:.1%}")
        print(f"    Recall preservation:     {q['recall_preservation']:.1%}")
    print()


def save_results(data: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    budget_str = f"_{data['budget']}b" if data['budget'] else ""
    filename = f"benchmark_{data['scenario']}{budget_str}.json"
    path = output_dir / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Results saved to {path}")

    # Save readable transcripts for side-by-side review
    transcripts_dir = output_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    for label in ("direct", "proxy"):
        transcript_path = transcripts_dir / f"{data['scenario']}{budget_str}_{label}.md"
        with open(transcript_path, "w", encoding="utf-8") as tf:
            tf.write(f"# {data['scenario']} — {label.upper()} transcript\n")
            tf.write(f"Model: {data['model']} | Budget: {data['budget']} | Turns: {data['turns_run']}\n\n")
            for t in data["turns"]:
                tf.write(f"---\n## Turn {t['turn']}\n\n")
                tf.write(f"**User:** {t.get('user_msg', t['user_msg_preview'])}\n\n")
                resp = t[label].get("response", t[label].get("response_preview", ""))
                tf.write(f"**{label.title()}** ({t[label]['output_tokens']} tokens, "
                         f"{t[label]['input_tokens']} in, {t[label]['latency_ms']:.0f}ms):\n\n")
                tf.write(f"{resp}\n\n")
    print(f"Transcripts saved to {transcripts_dir}/")

    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Archolith proxy benchmark suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python scripts/benchmark.py --scenario scenarios/taskflow.json
  python scripts/benchmark.py --scenario scenarios/taskflow.json --budget 4000
  python scripts/benchmark.py --all --budgets 4000,8000,15000,32000
  python scripts/benchmark.py --list
""",
    )
    parser.add_argument("--scenario", type=Path, help="Path to scenario JSON file")
    parser.add_argument("--all", action="store_true", help="Run all scenarios in scenarios/")
    parser.add_argument("--list", action="store_true", help="List available scenarios and exit")
    parser.add_argument("--budget", type=int, default=None, help="Token budget (sets CONTEXT_TOKEN_BUDGET)")
    parser.add_argument("--budgets", type=str, default=None,
                        help="Comma-separated budgets for matrix run (e.g., 4000,8000,15000)")
    parser.add_argument("--turns", type=int, default=None, help="Limit number of turns to run")
    parser.add_argument("--proxy", default=PROXY_URL, help="Proxy URL")
    parser.add_argument("--direct", default=DIRECT_URL, help="Direct upstream URL")
    parser.add_argument("--model", default=MODEL, help="Model to use")
    parser.add_argument("--output-dir", type=Path, default=Path("scripts/results"),
                        help="Output directory for results (default: scripts/results)")
    parser.add_argument("--probes-only", action="store_true",
                        help="Only run fact probes, skip full turn comparison")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last checkpoint if available")
    parser.add_argument("--api-key", default=None,
                        help="API key for upstream (overrides UPSTREAM_API_KEY from .env)")
    args = parser.parse_args()

    if args.list:
        print("Available scenarios:")
        for p in list_scenarios():
            s = Scenario.from_file(p)
            print(f"  {p.name:<25} {s.name:<15} {len(s.turns)} turns  "
                  f"{len(s.fact_probes)} probes  {s.description[:60]}")
        return

    api_key = args.api_key or API_KEY
    if not api_key:
        print("ERROR: Set UPSTREAM_API_KEY in .env or pass --api-key", file=sys.stderr)
        sys.exit(1)
    print(f"  API key: ...{api_key[-8:]}")

    # Determine scenarios to run
    scenarios: list[Scenario] = []
    if args.all:
        scenarios = [Scenario.from_file(p) for p in list_scenarios()]
    elif args.scenario:
        scenarios = [Scenario.from_file(args.scenario)]
    else:
        parser.error("Specify --scenario <file> or --all")

    # Determine budgets
    budgets: list[int | None] = [args.budget]
    if args.budgets:
        budgets = [int(b.strip()) for b in args.budgets.split(",")]

    print(f"Benchmark suite: {len(scenarios)} scenario(s) x {len(budgets)} budget(s)")
    print(f"  Proxy:  {args.proxy}")
    print(f"  Direct: {args.direct}")
    print(f"  Model:  {args.model}")

    # Verify proxy is reachable
    with httpx.Client() as c:
        try:
            r = c.get(f"{_proxy_base(args.proxy)}/health", timeout=5)
            health = r.json()
            print(f"  Proxy health: {health}")
            if health.get("graph") != "connected":
                print("WARNING: Proxy graph not connected — assembly won't fire")
        except Exception as e:
            print(f"ERROR: Can't reach proxy: {e}", file=sys.stderr)
            sys.exit(1)

    # Run the matrix
    all_results = []
    for scenario in scenarios:
        for budget in budgets:
            print(f"\n{'#'*70}")
            print(f"  Running: {scenario.name} @ budget={budget or 'default'}")
            print(f"  {scenario.description}")
            print(f"{'#'*70}")

            data = run_benchmark(scenario, args.turns, args.proxy, args.direct, args.model, budget, args.output_dir, args.resume, api_key=api_key)
            print_summary(data)
            save_results(data, args.output_dir)
            all_results.append(data)

    # Print cross-scenario summary if multiple runs
    if len(all_results) > 1:
        print(f"\n{'='*90}")
        print("  CROSS-SCENARIO SUMMARY")
        print(f"{'='*90}")
        header = f"{'Scenario':<20} {'Budget':>8} {'Turns':>6} {'Savings':>10} {'Recall':>10}"
        print(header)
        print("-" * 60)
        for data in all_results:
            s = data["summary"]
            q = data.get("quality", {})
            recall_str = f"{q.get('avg_proxy_recall', 0):.0%}" if q else "N/A"
            print(
                f"{data['scenario']:<20} "
                f"{str(data['budget'] or 'default'):>8} "
                f"{data['turns_run']:>6} "
                f"{s['overall_savings_ratio']:.1%}      "
                f"{recall_str:>10}"
            )
        print()


if __name__ == "__main__":
    main()
