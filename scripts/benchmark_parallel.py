"""Parallel benchmark — compare proxy-rewritten vs raw passthrough.

Sends the same multi-turn conversation through:
  1. Direct NVIDIA API (baseline — full conversation history every time)
  2. Context-engine proxy (test — assembly/rewriting may compress)

After each turn, fetches the proxy trace to see what the engine did
(assembly mode, facts injected, token savings). Outputs a per-turn
comparison table at the end.

Usage:
    python scripts/benchmark_parallel.py [--turns N] [--proxy URL] [--direct URL]

Requires UPSTREAM_API_KEY and UPSTREAM_BASE_URL in .env or env vars.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

PROXY_URL = os.getenv("PROXY_URL", "http://localhost:9800/v1")
DIRECT_URL = os.getenv("UPSTREAM_BASE_URL", "https://integrate.api.nvidia.com/v1")
API_KEY = os.getenv("UPSTREAM_API_KEY", "")
MODEL = os.getenv("BENCHMARK_MODEL", "gpt-4o-mini")

# A coding-style multi-turn conversation that builds context over time.
# Each entry is a user message; the assistant response from turn N becomes
# part of the history for turn N+1.
SCENARIO = [
    # Turn 1: establish a project
    "I'm building a Python FastAPI service called 'taskflow' that manages "
    "background task queues. It uses Redis for the queue backend and PostgreSQL "
    "for task metadata. The service needs three endpoints: POST /tasks to submit "
    "a task, GET /tasks/{id} to check status, and DELETE /tasks/{id} to cancel. "
    "Can you outline the data model for the Task entity?",

    # Turn 2: implementation detail
    "Good. Now I need the Redis queue integration. Tasks should be enqueued as "
    "JSON payloads with the task ID, type, and payload fields. Workers poll the "
    "queue with BRPOP. Write the enqueue function and the worker loop skeleton.",

    # Turn 3: reference earlier context
    "The Task entity you defined earlier — I want to add a 'retries' field and "
    "a 'max_retries' config. When a worker fails, it should re-enqueue up to "
    "max_retries times with exponential backoff. Update the worker loop.",

    # Turn 4: cross-reference multiple earlier points
    "Now wire up the FastAPI endpoints. POST /tasks should create the Task in "
    "Postgres AND enqueue to Redis. GET /tasks/{id} reads from Postgres. "
    "DELETE /tasks/{id} should mark the task as cancelled in Postgres and "
    "remove it from the Redis queue if it hasn't started. Use the data model "
    "and queue functions we already defined.",

    # Turn 5: ask about something established earlier
    "What happens if Redis is down when we try to enqueue? I want the POST "
    "endpoint to still create the task in Postgres with status='pending_enqueue' "
    "and have a background reconciler that retries enqueuing failed tasks. "
    "Also, remind me what fields the Task entity has right now.",

    # Turn 6: test the recall / compression
    "Write pytest tests for the enqueue function and the worker retry logic. "
    "Mock Redis with fakeredis. The tests should cover: successful enqueue, "
    "enqueue when Redis is down (should raise), worker processes task, "
    "worker retries on failure up to max_retries, worker gives up after "
    "max_retries exceeded.",

    # Turn 7: deep reference
    "I realized the exponential backoff formula we discussed — can you show me "
    "the exact calculation again and also add jitter? Then update the reconciler "
    "to use the same backoff logic when retrying failed enqueues.",

    # Turn 8: summary/integration
    "Give me a summary of the full taskflow architecture: all the components, "
    "how they interact, the data flow from task submission through execution "
    "to completion, and the failure/retry paths. Include the Redis queue, "
    "Postgres, the API endpoints, the worker, and the reconciler.",

    # Turn 9: new subsystem — scheduling
    "I need to add scheduled tasks. A new POST /tasks/schedule endpoint accepts "
    "a cron expression and creates a ScheduledTask in Postgres. A scheduler "
    "process checks every 60 seconds for due tasks and enqueues them via the "
    "same Redis queue. Design the ScheduledTask model and the scheduler loop.",

    # Turn 10: cross-reference scheduling with retry
    "What happens if a scheduled task fails? It should use the same retry logic "
    "with exponential backoff and max_retries we built earlier. But scheduled "
    "tasks also need a 'next_run_at' field that updates after each execution. "
    "Show the updated ScheduledTask model and how the scheduler interacts with "
    "the existing worker retry path.",

    # Turn 11: observability
    "I want to add observability. Create a /metrics endpoint that returns: total "
    "tasks created, tasks completed, tasks failed, tasks retried, average "
    "processing time, queue depth, active workers, and scheduler status. "
    "Use an in-memory metrics dict that gets updated by the worker and scheduler. "
    "Also add structured logging with structlog to all components.",

    # Turn 12: authentication and authorization
    "Add API key authentication to all endpoints. The API key comes in the "
    "X-API-Key header. Store valid keys in a 'api_keys' Postgres table with "
    "fields: key_hash, name, created_at, expires_at, scopes (JSON array). "
    "The /tasks endpoints need 'tasks:write' scope, /tasks/{id} GET needs "
    "'tasks:read', /metrics needs 'admin:read'. Implement the auth middleware.",

    # Turn 13: reference auth + scheduling together
    "The /tasks/schedule endpoint — what scope should it require? I'm thinking "
    "'tasks:schedule' as a separate scope. Also, the scheduler process itself "
    "doesn't go through the API, right? Confirm it directly accesses Postgres "
    "and Redis. But I want the scheduler to log which scheduled tasks it fires "
    "using the structured logging we set up earlier.",

    # Turn 14: error handling deep-dive
    "Let's harden error handling across the whole system. For each component "
    "we've built — the API endpoints, the worker, the scheduler, the reconciler "
    "— list every failure mode and how it's handled. I want to make sure we're "
    "not silently dropping tasks. Include: Redis connection failures, Postgres "
    "connection failures, malformed task payloads, worker crashes mid-task, "
    "and scheduler clock skew.",

    # Turn 15: deployment and configuration
    "Write a docker-compose.yml for the whole stack: the FastAPI app, Redis, "
    "Postgres, the worker process, the scheduler process, and the reconciler. "
    "All config should come from environment variables. Include the settings "
    "we've discussed: max_retries, backoff parameters, scheduler interval, "
    "reconciler interval, Redis URL, Postgres DSN, and the API key settings.",

    # Turn 16: final integration test
    "Write an integration test that exercises the full lifecycle: create an API "
    "key, submit a task, verify it's queued, have the worker process it, check "
    "the status transitions (pending -> queued -> processing -> completed), "
    "verify metrics updated, then test the failure path: submit a task that "
    "fails, verify retries happen with backoff, verify it eventually hits "
    "max_retries and moves to 'failed' status. Use the Task model, queue "
    "functions, worker, and auth middleware we already built.",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def estimate_tokens(text: str | None) -> int:
    """Rough char/4 token estimate."""
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


def send_chat(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    messages: list[dict],
    model: str,
) -> tuple[str, float, dict]:
    """Send a chat completion request. Returns (response_text, latency_ms, raw_json)."""
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

    start = time.monotonic()
    resp = client.post(url, json=body, headers=headers, timeout=120)
    latency_ms = (time.monotonic() - start) * 1000

    if resp.status_code != 200:
        return f"[ERROR {resp.status_code}]: {resp.text[:300]}", latency_ms, {}

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    return text, latency_ms, data


def _proxy_base(proxy_url: str) -> str:
    """Strip /v1 suffix to get the root proxy URL for admin/trace endpoints."""
    return proxy_url.rstrip("/").removesuffix("/v1")


def get_proxy_trace(client: httpx.Client, proxy_url: str, session_id: str | None = None) -> dict:
    """Fetch trace for a specific session, or the most recent one."""
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


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def run_benchmark(max_turns: int, proxy_url: str, direct_url: str) -> list[dict]:
    """Run the parallel benchmark and return per-turn results."""
    results = []
    direct_history: list[dict] = []
    proxy_history: list[dict] = []
    proxy_session_id: str | None = None

    # System message for both paths
    system_msg = {
        "role": "system",
        "content": "You are a senior Python developer helping design and implement "
                   "a FastAPI service. Be concise but thorough. Use code blocks for code.",
    }
    direct_history.append(system_msg)
    proxy_history.append(system_msg)

    turns = SCENARIO[:max_turns]

    with httpx.Client() as client:
        for i, user_msg in enumerate(turns, 1):
            print(f"\n{'='*60}")
            print(f"  TURN {i}/{len(turns)}")
            print(f"  User: {user_msg[:80]}...")
            print(f"{'='*60}")

            # Build messages for this turn
            direct_history.append({"role": "user", "content": user_msg})
            proxy_history.append({"role": "user", "content": user_msg})

            direct_input_tokens = estimate_messages_tokens(direct_history)
            proxy_input_tokens = estimate_messages_tokens(proxy_history)

            # --- Direct call (baseline) ---
            print(f"  [direct] Sending {len(direct_history)} messages "
                  f"(~{direct_input_tokens} tokens)...")
            direct_text, direct_latency, direct_raw = send_chat(
                client, direct_url, API_KEY, direct_history, MODEL
            )
            direct_output_tokens = estimate_tokens(direct_text)
            print(f"  [direct] Got {direct_output_tokens} tokens in {direct_latency:.0f}ms")

            # --- Proxy call (test) ---
            print(f"  [proxy]  Sending {len(proxy_history)} messages "
                  f"(~{proxy_input_tokens} tokens)...")
            proxy_text, proxy_latency, proxy_raw = send_chat(
                client, proxy_url, API_KEY, proxy_history, MODEL
            )
            proxy_output_tokens = estimate_tokens(proxy_text)
            print(f"  [proxy]  Got {proxy_output_tokens} tokens in {proxy_latency:.0f}ms")

            # --- Fetch proxy trace (specific turn) ---
            time.sleep(3)  # Let extraction finish
            trace = get_proxy_trace(client, proxy_url, session_id=proxy_session_id)
            trace_turns = trace.get("turns", [])

            # Find this turn's trace (turn_number = i-1 since proxy is 0-indexed)
            expected_turn = i - 1
            this_trace = {}
            for t in reversed(trace_turns):
                if t.get("turn_number") == expected_turn:
                    this_trace = t
                    break
            if not this_trace and trace_turns:
                this_trace = trace_turns[-1]  # Fallback to latest

            # Capture session_id from first trace
            if not proxy_session_id and trace.get("summary", {}).get("session_id"):
                proxy_session_id = trace["summary"]["session_id"]
                print(f"  [trace]  session_id={proxy_session_id}")

            assembly_mode = this_trace.get("assembly_mode", "unknown")
            savings_tokens = this_trace.get("savings_tokens", 0)
            savings_ratio = this_trace.get("savings_ratio", 0.0)
            facts_count = this_trace.get("facts_stored", 0)
            rewritten_tokens = this_trace.get("rewritten_tokens", 0)
            input_tokens_trace = this_trace.get("input_tokens", 0)

            print(f"  [trace]  assembly={assembly_mode}, "
                  f"input={input_tokens_trace}, rewritten={rewritten_tokens}, "
                  f"savings={savings_tokens} ({savings_ratio:.1%}), "
                  f"facts_stored={facts_count}")

            # Append assistant responses to histories
            direct_history.append({"role": "assistant", "content": direct_text})
            proxy_history.append({"role": "assistant", "content": proxy_text})

            result = {
                "turn": i,
                "user_msg_preview": user_msg[:60],
                "direct": {
                    "input_tokens": direct_input_tokens,
                    "output_tokens": direct_output_tokens,
                    "latency_ms": round(direct_latency, 1),
                    "response_preview": direct_text[:120] if direct_text else "",
                },
                "proxy": {
                    "input_tokens": proxy_input_tokens,
                    "output_tokens": proxy_output_tokens,
                    "latency_ms": round(proxy_latency, 1),
                    "response_preview": proxy_text[:120] if proxy_text else "",
                },
                "trace": {
                    "assembly_mode": assembly_mode,
                    "input_tokens": input_tokens_trace,
                    "rewritten_tokens": rewritten_tokens,
                    "savings_tokens": savings_tokens,
                    "savings_ratio": round(savings_ratio, 4),
                    "facts_stored": facts_count,
                    "session_id": proxy_session_id or "",
                },
            }
            results.append(result)

    return results


def print_summary(results: list[dict]) -> None:
    """Print a comparison table."""
    print("\n" + "=" * 90)
    print("  BENCHMARK SUMMARY")
    print("=" * 90)

    header = (
        f"{'Turn':>4}  {'Direct In':>10}  {'Proxy In':>10}  {'Rewritten':>10}  "
        f"{'Savings':>12}  {'Assembly':>18}  {'Facts':>5}  {'D ms':>8}  {'P ms':>8}"
    )
    print(header)
    print("-" * 100)

    total_direct_tokens = 0
    total_proxy_savings = 0

    for r in results:
        d = r["direct"]
        p = r["proxy"]
        t = r["trace"]

        total_direct_tokens += d["input_tokens"]
        total_proxy_savings += t["savings_tokens"]

        savings_str = f"{t['savings_tokens']:>5} ({t['savings_ratio']:.0%})"
        print(
            f"{r['turn']:>4}  "
            f"{d['input_tokens']:>10}  "
            f"{t.get('input_tokens', p['input_tokens']):>10}  "
            f"{t.get('rewritten_tokens', 0):>10}  "
            f"{savings_str:>12}  "
            f"{t['assembly_mode']:>18}  "
            f"{t['facts_stored']:>5}  "
            f"{d['latency_ms']:>8.0f}  "
            f"{p['latency_ms']:>8.0f}"
        )

    print("-" * 100)
    print(f"  Total direct input tokens: {total_direct_tokens}")
    print(f"  Total proxy savings:       {total_proxy_savings}")
    if total_direct_tokens > 0:
        print(f"  Overall savings ratio:     {total_proxy_savings/total_direct_tokens:.1%}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Context-engine parallel benchmark")
    parser.add_argument("--turns", type=int, default=len(SCENARIO),
                        help=f"Number of turns to run (max {len(SCENARIO)})")
    parser.add_argument("--proxy", default=PROXY_URL, help="Proxy URL")
    parser.add_argument("--direct", default=DIRECT_URL, help="Direct upstream URL")
    parser.add_argument("--output", default=None, help="Save JSON results to file")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: UPSTREAM_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    print(f"Benchmark: {args.turns} turns")
    print(f"  Proxy:  {args.proxy}")
    print(f"  Direct: {args.direct}")
    print(f"  Model:  {MODEL}")

    # Verify both endpoints
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

    results = run_benchmark(args.turns, args.proxy, args.direct)
    print_summary(results)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
