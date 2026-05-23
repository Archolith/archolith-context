"""Compare experiment results side-by-side.

Usage:
    python scripts/compare_experiments.py baseline tail5
    python scripts/compare_experiments.py baseline tail5 extract16k --dir scripts/results
    python scripts/compare_experiments.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_experiment(experiments_dir: Path, name: str) -> dict:
    path = experiments_dir / name / "experiment.json"
    if not path.exists():
        print(f"ERROR: Experiment '{name}' not found at {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def list_experiments(experiments_dir: Path) -> None:
    if not experiments_dir.exists():
        print("No experiments directory found.")
        return
    dirs = sorted(d for d in experiments_dir.iterdir() if d.is_dir() and (d / "experiment.json").exists())
    if not dirs:
        print("No experiments found.")
        return
    print(f"{'Experiment':<25} {'Date':<22} {'Scenarios':>10} {'Budgets':>10}")
    print("-" * 70)
    for d in dirs:
        meta = json.load(open(d / "experiment.json"))
        date = meta.get("started_at", "?")[:19]
        scenarios = len(meta.get("scenarios", []))
        budgets = ",".join(str(b) for b in meta.get("budgets", []))
        print(f"{d.name:<25} {date:<22} {scenarios:>10} {budgets:>10}")


def diff_configs(configs: list[tuple[str, dict]]) -> None:
    """Print config differences between experiments."""
    if len(configs) < 2:
        return
    all_keys = sorted(set().union(*(c.keys() for _, c in configs)))
    diffs = []
    for key in all_keys:
        values = [c.get(key) for _, c in configs]
        if len(set(str(v) for v in values)) > 1:
            diffs.append((key, values))

    if diffs:
        print(f"\n  CONFIG DIFFERENCES:")
        header = f"  {'Setting':<35}" + "".join(f" {name:>15}" for name, _ in configs)
        print(header)
        print("  " + "-" * (35 + 16 * len(configs)))
        for key, values in diffs:
            vals = "".join(f" {str(v):>15}" for v in values)
            print(f"  {key:<35}{vals}")
    else:
        print("\n  All configs identical.")


def compare(experiments: list[dict]) -> None:
    names = [e["experiment"] for e in experiments]

    # Config diff
    configs = [(e["experiment"], e.get("proxy_config", {})) for e in experiments]
    diff_configs(configs)

    # Collect all (scenario, budget) pairs
    all_keys = set()
    result_maps = []
    for exp in experiments:
        rmap = {}
        for r in exp.get("results_summary", []):
            key = (r["scenario"], r.get("budget"))
            rmap[key] = r
            all_keys.add(key)
        result_maps.append(rmap)

    all_keys = sorted(all_keys)

    # Side-by-side results
    print(f"\n  RESULTS COMPARISON")
    print(f"  {'Scenario':<18} {'Budget':>7}", end="")
    for name in names:
        print(f"  | {'Savings':>8} {'Recall':>8} {'Tokens':>8}", end="")
    print()
    print("  " + "-" * (27 + 30 * len(names)))

    for scenario, budget in all_keys:
        print(f"  {scenario:<18} {str(budget or 'def'):>7}", end="")
        for rmap in result_maps:
            r = rmap.get((scenario, budget))
            if r:
                savings = f"{r['savings_ratio']:.1%}"
                recall = f"{r.get('avg_proxy_recall', 0):.0%}" if r.get("avg_proxy_recall") is not None else "N/A"
                tokens = f"{r.get('proxy_tokens', 0):,}"
                print(f"  | {savings:>8} {recall:>8} {tokens:>8}", end="")
            else:
                print(f"  | {'—':>8} {'—':>8} {'—':>8}", end="")
        print()

    # Summary: average across all runs
    print()
    print(f"  {'AVERAGE':<18} {'':>7}", end="")
    for rmap in result_maps:
        if rmap:
            vals = list(rmap.values())
            avg_savings = sum(r["savings_ratio"] for r in vals) / len(vals)
            recalls = [r["avg_proxy_recall"] for r in vals if r.get("avg_proxy_recall") is not None]
            avg_recall = sum(recalls) / len(recalls) if recalls else 0
            avg_tokens = sum(r.get("proxy_tokens", 0) for r in vals) / len(vals)
            print(f"  | {avg_savings:>7.1%} {avg_recall:>7.0%} {avg_tokens:>8.0f}", end="")
        else:
            print(f"  | {'—':>8} {'—':>8} {'—':>8}", end="")
    print("\n")


def main():
    parser = argparse.ArgumentParser(description="Compare benchmark experiments")
    parser.add_argument("experiments", nargs="*", help="Experiment names to compare")
    parser.add_argument("--dir", type=Path, default=Path("scripts/results"),
                        help="Base results directory (default: scripts/results)")
    parser.add_argument("--list", action="store_true", help="List available experiments")
    args = parser.parse_args()

    experiments_dir = args.dir / "experiments"

    if args.list:
        list_experiments(experiments_dir)
        return

    if len(args.experiments) < 2:
        parser.error("Provide at least 2 experiment names to compare (or use --list)")

    experiments = [load_experiment(experiments_dir, name) for name in args.experiments]
    compare(experiments)


if __name__ == "__main__":
    main()
