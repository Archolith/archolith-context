"""ContextBench integration harness for archolith-proxy benchmarking.

Runs ContextBench agent instances twice — once direct, once through the proxy —
then evaluates both trajectories against gold annotations and produces a
side-by-side comparison of context retrieval quality and token savings.

This is the primary benchmark for proving the proxy's value proposition:
"compressed context doesn't lose the important stuff."

Prerequisites:
  - ContextBench cloned at CONTEXTBENCH_DIR (default: projects/forked/ContextBench)
  - ContextBench dependencies installed (pip install -r requirements.txt)
  - Proxy running at PROXY_URL
  - OPENAI_API_KEY / UPSTREAM_API_KEY set

Usage:
    python scripts/contextbench_harness.py --agent agentless --bench Verified --limit 5
    python scripts/contextbench_harness.py --agent agentless --bench Verified --limit 50 --budget 4000
    python scripts/contextbench_harness.py --evaluate-only --direct-traj results/direct --proxy-traj results/proxy
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:9800/v1")
DIRECT_URL = os.getenv("UPSTREAM_BASE_URL", "https://api.openai.com/v1")
API_KEY = os.getenv("UPSTREAM_API_KEY", os.getenv("OPENAI_API_KEY", ""))

CONTEXTBENCH_DIR = Path(os.getenv(
    "CONTEXTBENCH_DIR",
    Path(__file__).resolve().parent.parent.parent.parent / "forked" / "ContextBench",
))
GOLD_DATA = CONTEXTBENCH_DIR / "data" / "contextbench_verified.parquet"


def _proxy_base(proxy_url: str) -> str:
    return proxy_url.rstrip("/").removesuffix("/v1")


def check_proxy(proxy_url: str) -> bool:
    base = _proxy_base(proxy_url)
    try:
        with httpx.Client() as c:
            r = c.get(f"{base}/health", timeout=5)
            health = r.json()
            print(f"  Proxy health: {health}")
            return health.get("graph") == "connected"
    except Exception as e:
        print(f"  Proxy unreachable: {e}")
        return False


def set_proxy_budget(proxy_url: str, budget: int) -> bool:
    base = _proxy_base(proxy_url)
    try:
        with httpx.Client() as c:
            r = c.post(f"{base}/admin/config", json={"context_token_budget": budget}, timeout=5)
            return r.status_code == 200
    except Exception:
        return False


def get_proxy_session_summary(proxy_url: str) -> dict:
    """Get aggregate session stats from the proxy trace API."""
    base = _proxy_base(proxy_url)
    try:
        with httpx.Client() as c:
            r = c.get(f"{base}/trace/sessions", timeout=10)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Agent run orchestration
# ---------------------------------------------------------------------------

def run_agent(
    agent: str,
    bench: str,
    base_url: str,
    api_key: str,
    model: str,
    output_dir: Path,
    contextbench_dir: Path,
    limit: int = 0,
    instances: str = "",
    timeout_per_instance: int = 1800,
) -> int:
    """Run ContextBench agent with the given base URL. Returns exit code."""
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "contextbench.run",
        "--agent", agent,
        "--bench", bench,
        "--output", str(output_dir),
        "--timeout", str(timeout_per_instance),
    ]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    if instances:
        cmd += ["--instances", instances]

    env = os.environ.copy()
    env["OPENAI_BASE_URL"] = base_url
    env["OPENAI_API_KEY"] = api_key
    env["OPENAI_MODEL"] = model

    print(f"\n  Running {agent} via {base_url}")
    print(f"  Output: {output_dir}")
    print(f"  Command: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        cwd=str(contextbench_dir),
        env=env,
        timeout=timeout_per_instance * (limit or 500) + 600,
    )
    return result.returncode


def find_trajectories(output_dir: Path) -> list[Path]:
    """Find trajectory JSON files in the output directory."""
    trajs = []
    for ext in ("*.traj.json", "*.json", "*.jsonl"):
        trajs.extend(output_dir.rglob(ext))
    return sorted(set(trajs))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def run_evaluation(
    gold_path: Path,
    pred_path: Path,
    out_path: Path,
    cache_dir: Path,
    contextbench_dir: Path | None = None,
) -> int:
    """Run contextbench.evaluate on a trajectory file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "contextbench.evaluate",
        "--gold", str(gold_path),
        "--pred", str(pred_path),
        "--cache", str(cache_dir),
        "--out", str(out_path),
    ]

    print(f"\n  Evaluating: {pred_path.name}")
    print(f"  Gold: {gold_path}")
    print(f"  Output: {out_path}")

    result = subprocess.run(cmd, cwd=str(contextbench_dir or CONTEXTBENCH_DIR))
    return result.returncode


def load_results(path: Path) -> list[dict]:
    """Load JSONL evaluation results."""
    results = []
    if not path.exists():
        return results
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                results.append(json.loads(line))
    return results


def compare_results(direct_results: list[dict], proxy_results: list[dict]) -> dict:
    """Compare direct vs proxy evaluation results."""
    direct_by_id = {r.get("instance_id", ""): r for r in direct_results if "error" not in r}
    proxy_by_id = {r.get("instance_id", ""): r for r in proxy_results if "error" not in r}

    common_ids = sorted(set(direct_by_id) & set(proxy_by_id))
    if not common_ids:
        return {"error": "no common instances to compare", "direct_count": len(direct_by_id), "proxy_count": len(proxy_by_id)}

    granularities = ["file", "symbol", "span", "line"]
    comparison = {
        "instances_compared": len(common_ids),
        "direct_total": len(direct_results),
        "proxy_total": len(proxy_results),
        "direct_errors": sum(1 for r in direct_results if "error" in r),
        "proxy_errors": sum(1 for r in proxy_results if "error" in r),
    }

    for gran in granularities:
        d_intersections = []
        d_gold_sizes = []
        d_pred_sizes = []
        p_intersections = []
        p_gold_sizes = []
        p_pred_sizes = []

        for iid in common_ids:
            d_final = direct_by_id[iid].get("final", {}).get(gran, {})
            p_final = proxy_by_id[iid].get("final", {}).get(gran, {})

            if d_final and p_final:
                d_intersections.append(d_final.get("intersection", 0))
                d_gold_sizes.append(d_final.get("gold_size", 0))
                d_pred_sizes.append(d_final.get("pred_size", 0))
                p_intersections.append(p_final.get("intersection", 0))
                p_gold_sizes.append(p_final.get("gold_size", 0))
                p_pred_sizes.append(p_final.get("pred_size", 0))

        if d_gold_sizes:
            d_total_inter = sum(d_intersections)
            d_total_gold = sum(d_gold_sizes)
            d_total_pred = sum(d_pred_sizes)
            p_total_inter = sum(p_intersections)
            p_total_gold = sum(p_gold_sizes)
            p_total_pred = sum(p_pred_sizes)

            d_coverage = d_total_inter / d_total_gold if d_total_gold else 0
            d_precision = d_total_inter / d_total_pred if d_total_pred else 0
            p_coverage = p_total_inter / p_total_gold if p_total_gold else 0
            p_precision = p_total_inter / p_total_pred if p_total_pred else 0

            preservation = p_coverage / d_coverage if d_coverage > 0 else 0

            comparison[gran] = {
                "direct_coverage": round(d_coverage, 4),
                "direct_precision": round(d_precision, 4),
                "proxy_coverage": round(p_coverage, 4),
                "proxy_precision": round(p_precision, 4),
                "coverage_preservation": round(preservation, 4),
                "instances_with_data": len(d_gold_sizes),
            }

    return comparison


def print_comparison(comparison: dict, budget: int | None = None) -> None:
    """Print a formatted comparison table."""
    print(f"\n{'='*80}")
    print(f"  CONTEXTBENCH COMPARISON (budget={budget or 'default'})")
    print(f"{'='*80}")
    print(f"  Instances compared: {comparison.get('instances_compared', 0)}")
    print(f"  Direct errors: {comparison.get('direct_errors', 0)}")
    print(f"  Proxy errors:  {comparison.get('proxy_errors', 0)}")

    granularities = ["file", "symbol", "span", "line"]
    header = f"{'Granularity':<10} {'Direct Cov':>11} {'Proxy Cov':>11} {'Preservation':>13} {'Direct Prec':>12} {'Proxy Prec':>12}"
    print(f"\n{header}")
    print("-" * 75)

    for gran in granularities:
        g = comparison.get(gran, {})
        if not g:
            continue
        print(
            f"{gran:<10} "
            f"{g['direct_coverage']:>10.1%} "
            f"{g['proxy_coverage']:>10.1%} "
            f"{g['coverage_preservation']:>12.1%} "
            f"{g['direct_precision']:>11.1%} "
            f"{g['proxy_precision']:>11.1%}"
        )

    print(f"{'='*80}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ContextBench integration harness for archolith-proxy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--agent", default="agentless", help="Agent framework (default: agentless)")
    parser.add_argument("--bench", default="Verified", help="Benchmark subset (default: Verified)")
    parser.add_argument("--limit", type=int, default=5, help="Max instances to run (default: 5)")
    parser.add_argument("--instances", type=str, default="", help="Specific instance IDs (comma-separated)")
    parser.add_argument("--model", default=os.getenv("BENCHMARK_MODEL", "gpt-4o-mini"), help="Model to use")
    parser.add_argument("--budget", type=int, default=None, help="Proxy token budget")
    parser.add_argument("--proxy", default=PROXY_URL, help="Proxy URL")
    parser.add_argument("--direct", default=DIRECT_URL, help="Direct upstream URL")
    parser.add_argument("--output-dir", type=Path, default=Path("scripts/results/contextbench"),
                        help="Output directory")
    parser.add_argument("--gold", type=Path, default=GOLD_DATA, help="Gold annotations path")
    parser.add_argument("--cache", type=Path, default=Path("./repos"), help="Repo cache directory")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout per instance (seconds)")
    parser.add_argument("--contextbench-dir", type=Path, default=CONTEXTBENCH_DIR,
                        help="Path to ContextBench clone")

    # Evaluation-only mode
    parser.add_argument("--evaluate-only", action="store_true",
                        help="Skip agent runs, only evaluate existing trajectories")
    parser.add_argument("--direct-traj", type=Path, default=None,
                        help="Path to direct trajectory dir (for --evaluate-only)")
    parser.add_argument("--proxy-traj", type=Path, default=None,
                        help="Path to proxy trajectory dir (for --evaluate-only)")

    args = parser.parse_args()

    contextbench_dir = args.contextbench_dir

    if not contextbench_dir.exists():
        print(f"ERROR: ContextBench not found at {contextbench_dir}", file=sys.stderr)
        sys.exit(1)

    if not args.gold.exists():
        print(f"ERROR: Gold data not found at {args.gold}", file=sys.stderr)
        sys.exit(1)

    budget_str = f"_{args.budget}b" if args.budget else ""
    direct_output = args.output_dir / f"direct{budget_str}" / args.agent
    proxy_output = args.output_dir / f"proxy{budget_str}" / args.agent
    direct_eval = args.output_dir / f"eval_direct{budget_str}.jsonl"
    proxy_eval = args.output_dir / f"eval_proxy{budget_str}.jsonl"
    comparison_path = args.output_dir / f"comparison{budget_str}.json"

    if not args.evaluate_only:
        # Check proxy
        print("Checking proxy...")
        if not check_proxy(args.proxy):
            print("ERROR: Proxy not healthy — assembly won't work", file=sys.stderr)
            sys.exit(1)

        # Set budget
        if args.budget:
            if set_proxy_budget(args.proxy, args.budget):
                print(f"  Proxy budget set to {args.budget}")
            else:
                print("  WARNING: Could not set proxy budget")

        print(f"\n{'#'*70}")
        print(f"  Phase 1: DIRECT run ({args.agent} → {args.direct})")
        print(f"{'#'*70}")
        rc = run_agent(
            agent=args.agent,
            bench=args.bench,
            base_url=args.direct,
            api_key=API_KEY,
            model=args.model,
            output_dir=direct_output,
            contextbench_dir=contextbench_dir,
            limit=args.limit,
            instances=args.instances,
            timeout_per_instance=args.timeout,
        )
        if rc != 0:
            print(f"WARNING: Direct run exited with code {rc}")

        print(f"\n{'#'*70}")
        print(f"  Phase 2: PROXY run ({args.agent} → {args.proxy})")
        print(f"{'#'*70}")
        rc = run_agent(
            agent=args.agent,
            bench=args.bench,
            base_url=args.proxy,
            api_key=API_KEY,
            model=args.model,
            output_dir=proxy_output,
            contextbench_dir=contextbench_dir,
            limit=args.limit,
            instances=args.instances,
            timeout_per_instance=args.timeout,
        )
        if rc != 0:
            print(f"WARNING: Proxy run exited with code {rc}")
    else:
        direct_output = args.direct_traj or direct_output
        proxy_output = args.proxy_traj or proxy_output

    # Find trajectories
    direct_trajs = find_trajectories(direct_output)
    proxy_trajs = find_trajectories(proxy_output)
    print(f"\n  Found {len(direct_trajs)} direct trajectories, {len(proxy_trajs)} proxy trajectories")

    if not direct_trajs or not proxy_trajs:
        print("ERROR: Need trajectories from both direct and proxy runs to compare")
        sys.exit(1)

    # Evaluate
    print(f"\n{'#'*70}")
    print(f"  Phase 3: EVALUATION")
    print(f"{'#'*70}")

    for traj_path in direct_trajs:
        run_evaluation(args.gold, traj_path, direct_eval, args.cache, contextbench_dir)

    for traj_path in proxy_trajs:
        run_evaluation(args.gold, traj_path, proxy_eval, args.cache, contextbench_dir)

    # Compare
    print(f"\n{'#'*70}")
    print(f"  Phase 4: COMPARISON")
    print(f"{'#'*70}")

    direct_results = load_results(direct_eval)
    proxy_results = load_results(proxy_eval)

    comparison = compare_results(direct_results, proxy_results)
    comparison["budget"] = args.budget
    comparison["agent"] = args.agent
    comparison["bench"] = args.bench
    comparison["model"] = args.model
    comparison["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    print_comparison(comparison, args.budget)

    # Save
    with open(comparison_path, "w") as f:
        json.dump(comparison, f, indent=2)
    print(f"  Comparison saved to {comparison_path}")

    # Capture proxy token savings
    sessions = get_proxy_session_summary(args.proxy)
    if sessions:
        comparison["proxy_sessions"] = sessions
        with open(comparison_path, "w") as f:
            json.dump(comparison, f, indent=2)

    # Headline result
    file_metrics = comparison.get("file", {})
    if file_metrics:
        pres = file_metrics.get("coverage_preservation", 0)
        print(f"\n  HEADLINE: {pres:.0%} file-level coverage preservation under proxy compression")
        if pres >= 0.95:
            print("  STATUS: PASS (target: ≥95%)")
        else:
            print(f"  STATUS: BELOW TARGET (got {pres:.0%}, need ≥95%)")


if __name__ == "__main__":
    main()
