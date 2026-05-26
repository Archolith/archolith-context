"""Interactive benchmark driver — human-driven proxy observation.

Step through a scenario turn by turn, seeing what the proxy assembles
and how it affects the model's response. The operator drives: continue,
inject custom turns, inspect context, change budget, or abort.

Usage:
    python scripts/interactive_benchmark.py --scenario scenarios/code_review.json
    python scripts/interactive_benchmark.py --scenario scenarios/debugging.json --budget 8000
    python scripts/interactive_benchmark.py --freeform  # no scenario, just type messages

Commands between turns:
    [Enter] / n     next scripted turn
    <text>          inject custom user message instead of next turn
    t               show full trace for last turn (assembly, facts, rewrite)
    c               show assembled context that was sent to the model
    d               show direct (baseline) response in full
    p               show proxy response in full
    r               show rewritten messages (what proxy actually sent upstream)
    f               show facts in the graph for this session
    b <budget>      change the token budget
    s               skip next scripted turn
    q               quit and save transcript
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:9800/v1")
DIRECT_URL = os.getenv("UPSTREAM_BASE_URL", "https://integrate.api.nvidia.com/v1")
API_KEY = os.getenv("UPSTREAM_API_KEY", "")
MODEL = os.getenv("BENCHMARK_MODEL", "gpt-4o-mini")

COLLAPSE_THRESHOLD = 50
WIDTH = 100


def _proxy_base(proxy_url: str) -> str:
    return proxy_url.rstrip("/").removesuffix("/v1")


def _wrap(text: str, indent: int = 4, width: int = WIDTH) -> str:
    prefix = " " * indent
    lines = text.split("\n")
    wrapped = []
    for line in lines:
        if len(line) + indent <= width:
            wrapped.append(prefix + line)
        else:
            for wl in textwrap.wrap(line, width=width - indent):
                wrapped.append(prefix + wl)
    return "\n".join(wrapped)


def _truncate(text: str, max_chars: int = 500) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n    [...{len(text) - max_chars} more chars...]"


def _separator(char: str = "=", label: str = "") -> str:
    if label:
        pad = (WIDTH - len(label) - 2) // 2
        return f"{char * pad} {label} {char * pad}"
    return char * WIDTH


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def send_chat(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    messages: list[dict],
    model: str,
) -> tuple[str, float, dict]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"model": model, "messages": messages, "max_tokens": 2048, "temperature": 0.3}

    for attempt in range(4):
        start = time.monotonic()
        try:
            resp = client.post(url, json=body, headers=headers, timeout=300)
        except httpx.TimeoutException:
            return "[TIMEOUT]", (time.monotonic() - start) * 1000, {}
        except httpx.HTTPError as e:
            return f"[HTTP ERROR]: {e}", (time.monotonic() - start) * 1000, {}

        if resp.status_code == 429 and attempt < 3:
            retry_after = resp.headers.get("Retry-After", "30")
            try:
                wait = min(int(retry_after), 120)
            except ValueError:
                wait = 30
            print(f"  [429] Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        break

    latency = (time.monotonic() - start) * 1000
    if resp.status_code != 200:
        return f"[ERROR {resp.status_code}]: {resp.text[:300]}", latency, {}

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    usage = data.get("usage", {})
    return text, latency, usage


def get_trace(client: httpx.Client, proxy_url: str, session_id: str | None = None) -> dict:
    base = _proxy_base(proxy_url)
    try:
        if not session_id:
            resp = client.get(f"{base}/trace/sessions", timeout=10)
            if resp.status_code != 200:
                return {}
            sessions = resp.json().get("sessions", [])
            if not sessions:
                return {}
            session_id = sessions[0]["session_id"]

        resp = client.get(f"{base}/trace/sessions/{session_id}", timeout=10)
        if resp.status_code != 200:
            return {}
        return resp.json()
    except Exception:
        return {}


def get_graph_facts(client: httpx.Client, proxy_url: str, session_id: str) -> list[dict]:
    base = _proxy_base(proxy_url)
    try:
        resp = client.get(f"{base}/trace/graph/{session_id}/facts?limit=200", timeout=10)
        if resp.status_code == 200:
            return resp.json().get("facts", [])
    except Exception:
        pass
    return []


def set_budget(client: httpx.Client, proxy_url: str, budget: int) -> bool:
    base = _proxy_base(proxy_url)
    try:
        resp = client.post(f"{base}/admin/config", json={"context_token_budget": budget}, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def show_turn_summary(
    turn_num: int,
    user_msg: str,
    direct_text: str,
    direct_usage: dict,
    direct_latency: float,
    proxy_text: str,
    proxy_usage: dict,
    proxy_latency: float,
    trace_turn: dict,
) -> None:
    d_in = direct_usage.get("prompt_tokens", "?")
    d_out = direct_usage.get("completion_tokens", "?")
    p_in = proxy_usage.get("prompt_tokens", "?")
    p_out = proxy_usage.get("completion_tokens", "?")

    assembly = trace_turn.get("assembly_mode", "?")
    savings = trace_turn.get("savings_ratio", 0)
    facts_stored = trace_turn.get("facts_stored", 0)
    facts_selected = len(trace_turn.get("facts_selected", []))

    print(f"\n{_separator('=', f'TURN {turn_num}')}")
    print(f"\n  USER: {user_msg[:200]}")
    if len(user_msg) > 200:
        print(f"        [...{len(user_msg) - 200} more chars]")

    print(f"\n{_separator('-', 'DIRECT (baseline)')}")
    print(f"  tokens: {d_in} in / {d_out} out | {direct_latency:.0f}ms")
    print(_wrap(_truncate(direct_text, 400)))

    print(f"\n{_separator('-', 'PROXY')}")
    print(f"  tokens: {p_in} in / {p_out} out | {proxy_latency:.0f}ms")
    print(f"  assembly: {assembly} | savings: {savings:.0%} | facts: {facts_selected} selected, {facts_stored} stored")

    collapse = ""
    if isinstance(p_out, int) and p_out < COLLAPSE_THRESHOLD:
        collapse = "  !! OUTPUT COLLAPSE"
    print(f"  {collapse}")
    print(_wrap(_truncate(proxy_text, 400)))

    print(f"\n{_separator('-')}")


def show_trace_detail(trace_turn: dict) -> None:
    print(f"\n{_separator('=', 'TRACE DETAIL')}")
    for key in [
        "turn_number", "assembly_mode", "assembly_reason",
        "input_tokens", "rewritten_tokens", "savings_tokens", "savings_ratio",
        "assembly_latency_ms", "extraction_latency_ms",
        "facts_stored", "duplicates_skipped",
        "invalidations_attempted", "invalidations_matched",
        "upstream_status", "upstream_latency_ms", "output_tokens",
    ]:
        val = trace_turn.get(key)
        if val is not None:
            print(f"  {key}: {val}")

    facts = trace_turn.get("facts_selected", [])
    if facts:
        print(f"\n  FACTS SELECTED ({len(facts)}):")
        for f in facts[:20]:
            ftype = f.get("fact_type", "?")
            turn = f.get("source_turn", "?")
            content = f.get("content", "")[:120]
            conf = f.get("confidence", "?")
            print(f"    [{ftype}|t{turn}|c{conf}] {content}")
        if len(facts) > 20:
            print(f"    ...and {len(facts) - 20} more")

    extracted = trace_turn.get("extracted_facts", [])
    if extracted:
        print(f"\n  FACTS EXTRACTED THIS TURN ({len(extracted)}):")
        for f in extracted[:15]:
            ftype = f.get("fact_type", "?")
            content = f.get("content", "")[:120]
            print(f"    [{ftype}] {content}")
    print()


def show_assembled_context(trace_turn: dict) -> None:
    rewritten = trace_turn.get("rewritten_messages", [])
    if not rewritten:
        print("  No rewritten messages in trace.")
        return

    print(f"\n{_separator('=', 'ASSEMBLED CONTEXT')}")
    for i, msg in enumerate(rewritten):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role == "system":
            print(f"\n  [{i}] SYSTEM ({len(content)} chars):")
            print(_wrap(content[:2000]))
            if len(content) > 2000:
                print(f"    [...{len(content) - 2000} more chars]")
        else:
            preview = content[:300] if len(content) > 300 else content
            print(f"\n  [{i}] {role.upper()} ({len(content)} chars):")
            print(_wrap(preview))
            if len(content) > 300:
                print(f"    [...{len(content) - 300} more chars]")
    print()


def show_rewritten_messages(trace_turn: dict) -> None:
    rewritten = trace_turn.get("rewritten_messages", [])
    if not rewritten:
        print("  No rewritten messages in trace.")
        return

    print(f"\n{_separator('=', 'REWRITTEN MESSAGES')}")
    print(f"  {len(rewritten)} messages total\n")
    for i, msg in enumerate(rewritten):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        chars = len(content)
        print(f"  [{i:>2}] {role:<10} {chars:>6} chars", end="")
        if role == "tool":
            print(f"  tool_call_id={msg.get('tool_call_id', '?')}", end="")
        print()
    print()


def show_graph_facts(facts: list[dict]) -> None:
    if not facts:
        print("  No facts in graph.")
        return

    print(f"\n{_separator('=', f'FACT GRAPH ({len(facts)} facts)')}")
    by_turn: dict[int, list] = {}
    for f in facts:
        t = f.get("source_turn", 0)
        by_turn.setdefault(t, []).append(f)

    for turn_num in sorted(by_turn):
        turn_facts = by_turn[turn_num]
        print(f"\n  Turn {turn_num} ({len(turn_facts)} facts):")
        for f in turn_facts:
            ftype = f.get("fact_type", "?")
            conf = f.get("confidence", "?")
            content = f.get("content", "")[:150]
            valid = "expired" if f.get("valid_until") else "active"
            print(f"    [{ftype}|c{conf}|{valid}] {content}")
    print()


# ---------------------------------------------------------------------------
# Scenario loading (reuses benchmark format)
# ---------------------------------------------------------------------------

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_scenario(path: Path) -> dict:
    if not path.exists() and not path.is_absolute():
        candidate = SCENARIOS_DIR / path.name
        if candidate.exists():
            path = candidate
    with open(path) as f:
        data = json.load(f)
    return data


# ---------------------------------------------------------------------------
# Transcript saving
# ---------------------------------------------------------------------------

def save_transcript(transcript: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path.with_suffix(".json"), "w") as f:
        json.dump(transcript, f, indent=2)

    with open(output_path.with_suffix(".md"), "w", encoding="utf-8") as f:
        f.write("# Interactive Benchmark Transcript\n\n")
        for entry in transcript:
            f.write(f"---\n## Turn {entry['turn']}\n\n")
            f.write(f"**User:** {entry['user_msg']}\n\n")
            f.write(f"**Direct** ({entry['direct_tokens']} out, {entry['direct_latency']:.0f}ms):\n")
            f.write(f"{entry['direct_response']}\n\n")
            f.write(f"**Proxy** ({entry['proxy_tokens']} out, {entry['proxy_latency']:.0f}ms, "
                    f"assembly={entry['assembly_mode']}, savings={entry['savings_ratio']:.0%}):\n")
            f.write(f"{entry['proxy_response']}\n\n")
            if entry.get("operator_note"):
                f.write(f"**Operator note:** {entry['operator_note']}\n\n")

    print(f"\n  Transcript saved to {output_path.with_suffix('.md')}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_interactive(
    scenario_path: Path | None,
    proxy_url: str,
    direct_url: str,
    model: str,
    budget: int | None,
    api_key: str,
    output_dir: Path,
) -> None:
    scenario = None
    turns: list[str] = []
    scenario_name = "freeform"

    if scenario_path:
        scenario = load_scenario(scenario_path)
        turns = scenario.get("turns", [])
        scenario_name = scenario.get("name", scenario_path.stem)
        print(f"\n  Scenario: {scenario_name}")
        print(f"  Description: {scenario.get('description', '')}")
        print(f"  Turns: {len(turns)}")

    print(f"  Proxy: {proxy_url}")
    print(f"  Direct: {direct_url}")
    print(f"  Model: {model}")

    direct_history: list[dict] = []
    proxy_history: list[dict] = []
    transcript: list[dict] = []
    proxy_session_id: str | None = None
    turn_num = 0
    turn_index = 0
    last_trace_turn: dict = {}

    with httpx.Client() as client:
        # Health check
        try:
            base = _proxy_base(proxy_url)
            r = client.get(f"{base}/health", timeout=5)
            health = r.json()
            print(f"  Proxy health: {health}")
            if health.get("graph") != "connected":
                print("  WARNING: Graph not connected — assembly won't fire")
        except Exception as e:
            print(f"  ERROR: Can't reach proxy: {e}")
            return

        # Set budget
        if budget:
            if set_budget(client, proxy_url, budget):
                print(f"  Budget set to {budget}")
            else:
                print(f"  WARNING: Could not set budget")

        # System message
        system_prompt = ""
        if scenario:
            system_prompt = scenario.get("system_prompt", "You are a helpful assistant.")
        else:
            system_prompt = "You are a helpful assistant."

        direct_history.append({"role": "system", "content": system_prompt})
        proxy_history.append({"role": "system", "content": system_prompt})

        print(f"\n{_separator('=', 'INTERACTIVE BENCHMARK')}")
        print("  Commands: [Enter]=next  t=trace  c=context  d=direct  p=proxy")
        print("  r=rewritten msgs  f=graph facts  b <N>=budget  s=skip  q=quit")
        print("  Or type a message to inject as the next user turn.")
        print(_separator())

        while True:
            # Determine next user message
            next_scripted = None
            if turns and turn_index < len(turns):
                next_scripted = turns[turn_index]

            if next_scripted:
                prompt_label = f"\n  [{turn_index + 1}/{len(turns)}] "
                print(f"{prompt_label}Next: {next_scripted[:80]}...")
            elif turns:
                print(f"\n  [end of scenario — {len(turns)} turns complete]")
            else:
                print()

            try:
                cmd = input("  > ").strip()
            except (EOFError, KeyboardInterrupt):
                cmd = "q"

            # --- Command handling ---
            if cmd == "q":
                break

            if cmd == "t":
                show_trace_detail(last_trace_turn)
                continue

            if cmd == "c":
                show_assembled_context(last_trace_turn)
                continue

            if cmd == "d" and transcript:
                print(f"\n{_separator('-', 'DIRECT RESPONSE (full)')}")
                print(_wrap(transcript[-1]["direct_response"]))
                print()
                continue

            if cmd == "p" and transcript:
                print(f"\n{_separator('-', 'PROXY RESPONSE (full)')}")
                print(_wrap(transcript[-1]["proxy_response"]))
                print()
                continue

            if cmd == "r":
                show_rewritten_messages(last_trace_turn)
                continue

            if cmd == "f":
                if proxy_session_id:
                    facts = get_graph_facts(client, proxy_url, proxy_session_id)
                    show_graph_facts(facts)
                else:
                    print("  No session yet — send a turn first.")
                continue

            if cmd.startswith("b "):
                try:
                    new_budget = int(cmd.split()[1])
                    if set_budget(client, proxy_url, new_budget):
                        print(f"  Budget changed to {new_budget}")
                    else:
                        print(f"  Failed to set budget")
                except (ValueError, IndexError):
                    print("  Usage: b <number>")
                continue

            if cmd == "s":
                if next_scripted:
                    turn_index += 1
                    print(f"  Skipped turn {turn_index}")
                else:
                    print("  Nothing to skip")
                continue

            # Determine user message for this turn
            if cmd == "" or cmd == "n":
                if not next_scripted:
                    if turns:
                        print("  Scenario complete. Type a message or 'q' to quit.")
                        continue
                    else:
                        print("  Type a message to send.")
                        continue
                user_msg = next_scripted
                turn_index += 1
            else:
                user_msg = cmd

            turn_num += 1

            # Send to both
            direct_history.append({"role": "user", "content": user_msg})
            proxy_history.append({"role": "user", "content": user_msg})

            print(f"\n  Sending turn {turn_num}...")

            # Direct
            direct_text, direct_latency, direct_usage = send_chat(
                client, direct_url, api_key, direct_history, model,
            )

            # Proxy
            proxy_text, proxy_latency, proxy_usage = send_chat(
                client, proxy_url, api_key, proxy_history, model,
            )

            # Add to histories
            direct_history.append({"role": "assistant", "content": direct_text})
            proxy_history.append({"role": "assistant", "content": proxy_text})

            # Fetch trace
            time.sleep(2)
            trace = get_trace(client, proxy_url, session_id=proxy_session_id)
            trace_turns = trace.get("turns", [])

            # Find this turn's trace
            last_trace_turn = {}
            for tt in reversed(trace_turns):
                if tt.get("turn_number") == turn_num - 1:
                    last_trace_turn = tt
                    break
            if not last_trace_turn and trace_turns:
                last_trace_turn = trace_turns[-1]

            if not proxy_session_id and trace.get("summary", {}).get("session_id"):
                proxy_session_id = trace["summary"]["session_id"]

            # Display
            show_turn_summary(
                turn_num, user_msg,
                direct_text, direct_usage, direct_latency,
                proxy_text, proxy_usage, proxy_latency,
                last_trace_turn,
            )

            # Record
            p_out = proxy_usage.get("completion_tokens", 0)
            transcript.append({
                "turn": turn_num,
                "user_msg": user_msg,
                "direct_response": direct_text,
                "direct_tokens": direct_usage.get("completion_tokens", 0),
                "direct_latency": direct_latency,
                "proxy_response": proxy_text,
                "proxy_tokens": p_out,
                "proxy_latency": proxy_latency,
                "assembly_mode": last_trace_turn.get("assembly_mode", "?"),
                "savings_ratio": last_trace_turn.get("savings_ratio", 0),
                "facts_selected": len(last_trace_turn.get("facts_selected", [])),
                "facts_stored": last_trace_turn.get("facts_stored", 0),
                "operator_note": "",
            })

            # Warn on collapse
            if isinstance(p_out, int) and p_out < COLLAPSE_THRESHOLD:
                print(f"  !! WARNING: Proxy output collapsed to {p_out} tokens")
                print(f"  !! Type 'c' to inspect assembled context, 't' for trace detail")

    # Save on exit
    if transcript:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = output_dir / f"interactive_{scenario_name}_{ts}"
        save_transcript(transcript, out_path)

    print("\n  Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive benchmark driver — observe proxy behavior turn by turn",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scenario", type=Path, default=None,
                        help="Path to scenario JSON file (omit for freeform mode)")
    parser.add_argument("--freeform", action="store_true",
                        help="Freeform mode — no scenario, type all messages")
    parser.add_argument("--budget", type=int, default=None, help="Token budget")
    parser.add_argument("--proxy", default=PROXY_URL, help="Proxy URL")
    parser.add_argument("--direct", default=DIRECT_URL, help="Direct upstream URL")
    parser.add_argument("--model", default=MODEL, help="Model to use")
    parser.add_argument("--api-key", default=None, help="API key (overrides env)")
    parser.add_argument("--output-dir", type=Path, default=Path("scripts/results/interactive"),
                        help="Output directory for transcripts")
    args = parser.parse_args()

    api_key = args.api_key or API_KEY
    if not api_key:
        print("ERROR: Set UPSTREAM_API_KEY in .env or pass --api-key", file=sys.stderr)
        sys.exit(1)

    if not args.scenario and not args.freeform:
        parser.error("Specify --scenario <file> or --freeform")

    run_interactive(
        scenario_path=args.scenario,
        proxy_url=args.proxy,
        direct_url=args.direct,
        model=args.model,
        budget=args.budget,
        api_key=api_key,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
