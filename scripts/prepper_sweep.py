"""Prepper lever sweep — find the config that maximises briefing completion.

The background prepper at the default 12-iter budget returns ``no_result`` ~half
the time (model hits max_iterations without emitting the final context block).
This sweeps the two real levers — ``prepper_max_iterations`` and the prepper
system prompt's tool-call guidance ("converge sooner") — against a COPY of the
live graph DB, so the running proxy is untouched.

For each config it runs ``run_prepper`` K times against a graph-rich session and
reports: complete-rate (briefing cached), avg latency, avg tool calls, avg files.

Usage:
    python scripts/prepper_sweep.py [--session 953667d54af24918] [--samples 3]
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import statistics
import time
from pathlib import Path

# A "converge sooner" prompt variant: cap tool calls and reserve the final
# response for the context block (the default says "call tools 8-12 times").
_CONVERGE_REPLACEMENT = (
    "Call tools 5-7 times TOTAL across all iterations, then IMMEDIATELY emit the "
    "context block. Do NOT exceed 7 tool calls — always reserve your final "
    "response for the context block itself."
)


def _converge_prompt(default_prompt: str) -> str:
    needle = "Call tools 8-12 times across all iterations. Be comprehensive —\n   this is background compute with no time pressure."
    if needle in default_prompt:
        return default_prompt.replace(needle, _CONVERGE_REPLACEMENT)
    # Fallback: append the directive if the exact sentence drifted.
    return default_prompt + "\n\n" + _CONVERGE_REPLACEMENT


def _load_real_messages(trace_path: Path) -> tuple[list[dict], str]:
    """Load a realistic coherence tail from the session's trace JSONL.

    Picks the turn with the most original_messages (the richest context) and
    returns (messages, last_user_message) so the prepper does its real
    file-fetching work instead of short-circuiting on a thin synthetic input.
    """
    import json

    best_msgs: list[dict] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            t = json.loads(line)
        except Exception:
            continue
        om = t.get("original_messages") or []
        if len(om) > len(best_msgs):
            best_msgs = om
    user_msg = ""
    for m in reversed(best_msgs):
        if m.get("role") == "user":
            c = m.get("content")
            user_msg = c if isinstance(c, str) else json.dumps(c)
            break
    return best_msgs, user_msg[:4000]


async def _run_one(session_id: str, user_message: str, turn_number: int, messages: list[dict]):
    """Run a single prepper pass; return (complete, latency_ms, tool_calls, files)."""
    from archolith_proxy.curator.prepper import run_prepper

    t0 = time.monotonic()
    briefing = await run_prepper(
        session_id=session_id,
        turn_number=turn_number,
        user_message=user_message,
        session_goal=None,
        messages=messages,
    )
    latency_ms = (time.monotonic() - t0) * 1000
    if briefing is None:
        return (False, latency_ms, 0, 0)
    return (True, latency_ms, briefing.tool_calls_used, len(briefing.files))


async def _sweep(session_id: str, samples: int) -> None:
    from archolith_proxy.config import get_settings
    from archolith_proxy.graph.backend import close_backend, init_backend
    from archolith_proxy.graph.ladybug_backend import LadybugBackend
    import archolith_proxy.curator.prepper as prepper_mod

    settings = get_settings()
    default_prompt = prepper_mod.PREPPER_SYSTEM_PROMPT

    # Copy the live DB so the running proxy is untouched.
    src = Path(settings.ladybug_db_path)
    if not src.is_absolute():
        src = Path(__file__).parent.parent / src
    copy = src.with_name(f"sweep_copy_{int(time.time())}.lbug")
    shutil.copy2(src, copy)
    wal = src.with_suffix(src.suffix + ".wal")
    if wal.exists():
        shutil.copy2(wal, copy.with_suffix(copy.suffix + ".wal"))
    print(f"Copied DB -> {copy.name}")

    await init_backend(LadybugBackend(db_path=str(copy), max_concurrent_queries=settings.ladybug_max_concurrent))

    # Load a realistic coherence tail from the session's trace so the prepper
    # does its real file-fetching workload (the thing that causes live no_result).
    trace_path = Path(settings.trace_dir) / f"{session_id}.jsonl"
    real_messages: list[dict] = []
    probe = ("Add the next browse screen for the app, consistent with the existing "
             "screens. It lists items with a name and a value column.")
    if trace_path.exists():
        real_messages, real_user = _load_real_messages(trace_path)
        if real_user:
            probe = real_user
        print(f"Loaded coherence tail: {len(real_messages)} messages from {trace_path.name}")
    else:
        real_messages = [{"role": "user", "content": probe}]
        print(f"No trace at {trace_path}; using synthetic 1-message input (non-representative)")

    # Configs: (label, max_iterations, prompt)
    configs = [
        ("iters=12 default", 12, default_prompt),
        ("iters=16 default", 16, default_prompt),
        ("iters=20 default", 20, default_prompt),
        ("iters=12 converge", 12, _converge_prompt(default_prompt)),
        ("iters=16 converge", 16, _converge_prompt(default_prompt)),
    ]

    print(f"\nSession={session_id}  samples/config={samples}  budget_ms={settings.prepper_latency_budget_ms}")
    print(f"{'config':<20} {'complete':>9} {'avg_ms':>8} {'avg_tools':>10} {'avg_files':>10}")
    print("-" * 60)

    turn = 1000
    for label, iters, prompt in configs:
        settings.prepper_max_iterations = iters
        prepper_mod.PREPPER_SYSTEM_PROMPT = prompt
        results = []
        for _ in range(samples):
            turn += 1
            try:
                results.append(await _run_one(session_id, probe, turn, real_messages))
            except Exception as e:
                print(f"  run error ({label}): {type(e).__name__}: {e}")
                results.append((False, 0.0, 0, 0))
        completes = [r for r in results if r[0]]
        complete_rate = len(completes) / len(results) if results else 0.0
        avg_ms = statistics.mean(r[1] for r in results) if results else 0.0
        avg_tools = statistics.mean(r[2] for r in completes) if completes else 0.0
        avg_files = statistics.mean(r[3] for r in completes) if completes else 0.0
        print(f"{label:<20} {complete_rate*100:>7.0f}% {avg_ms:>8.0f} {avg_tools:>10.1f} {avg_files:>10.1f}")

    prepper_mod.PREPPER_SYSTEM_PROMPT = default_prompt
    await close_backend()

    # Clean up the copy.
    try:
        copy.unlink(missing_ok=True)
        copy.with_suffix(copy.suffix + ".wal").unlink(missing_ok=True)
    except Exception:
        pass
    print("\nDone. (DB copy removed.)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="953667d54af24918")
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()
    asyncio.run(_sweep(args.session, args.samples))


if __name__ == "__main__":
    main()
