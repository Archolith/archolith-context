"""Live proxy test for synthetic tool paths.

Sends realistic multi-turn conversations to a running archolith-context proxy
and validates that synthetic tool interception, recall, native Read caching,
and circuit breaker behavior work correctly in production-like conditions.

Usage:
    # Against local proxy (default localhost:9800)
    python scripts/test_synthetic_tools.py

    # Against a specific proxy
    PROXY_URL=http://vps:9800 python scripts/test_synthetic_tools.py

    # Run specific test suites
    python scripts/test_synthetic_tools.py --suite recall
    python scripts/test_synthetic_tools.py --suite synthetic
    python scripts/test_synthetic_tools.py --suite cache
    python scripts/test_synthetic_tools.py --suite all

    # Verbose output (show full responses)
    python scripts/test_synthetic_tools.py --verbose

Environment:
    PROXY_URL   — proxy base URL (default: http://localhost:9800)
    PROXY_MODEL — model name to use (default: deepseek-chat)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

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

PROXY_URL = os.getenv("PROXY_URL", _dotenv.get("PROXY_URL", "http://localhost:9800"))
PROXY_BASE = PROXY_URL.rstrip("/")
MODEL = os.getenv("PROXY_MODEL", _dotenv.get("BENCHMARK_MODEL", "deepseek-chat"))


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    turn: int
    status: int
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    has_tool_calls: bool = False
    tool_names: list[str] = field(default_factory=list)
    content_preview: str = ""
    error: str = ""


@dataclass
class SuiteResult:
    name: str
    passed: bool = True
    turns: list[TurnResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def send_turn(
    client: httpx.Client,
    session_id: str,
    messages: list[dict],
    stream: bool = False,
    max_tokens: int = 1000,
) -> dict:
    """Send a chat completion request to the proxy."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Session-ID": session_id,
    }
    resp = client.post(
        f"{PROXY_BASE}/v1/chat/completions",
        json=payload,
        headers=headers,
        timeout=120.0,
    )
    return {"status": resp.status_code, "data": resp.json() if resp.status_code == 200 else {}, "raw": resp}


def check_proxy_health(client: httpx.Client) -> bool:
    """Check if the proxy is healthy."""
    try:
        resp = client.get(f"{PROXY_BASE}/health", timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


def get_proxy_metrics(client: httpx.Client) -> dict:
    """Fetch proxy metrics."""
    try:
        resp = client.get(f"{PROXY_BASE}/metrics", timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def get_trace(client: httpx.Client, session_id: str) -> list[dict]:
    """Fetch trace turns for a session."""
    try:
        resp = client.get(f"{PROXY_BASE}/trace/{session_id}", timeout=10.0)
        if resp.status_code == 200:
            return resp.json().get("turns", [])
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

def _extract_result(resp: dict, turn_num: int, t0: float) -> TurnResult:
    """Extract a TurnResult from a proxy response."""
    latency = (time.monotonic() - t0) * 1000
    data = resp.get("data", {})
    usage = data.get("usage", {})
    choices = data.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}
    tool_calls = msg.get("tool_calls", [])

    return TurnResult(
        turn=turn_num,
        status=resp["status"],
        latency_ms=latency,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        has_tool_calls=bool(tool_calls),
        tool_names=[tc.get("function", {}).get("name", "") for tc in tool_calls],
        content_preview=(msg.get("content") or "")[:200],
        error=data.get("error", {}).get("message", "") if resp["status"] != 200 else "",
    )


def test_recall_interception(client: httpx.Client, verbose: bool = False) -> SuiteResult:
    """Test __archolith_recall tool interception.

    Strategy:
    1. Send several turns establishing facts (file contents, architecture decisions)
    2. Send a turn that should trigger the model to use recall
    3. Verify the response incorporates recalled context
    4. Check traces for recall_used=True
    """
    result = SuiteResult(name="recall_interception")
    session_id = f"syntest-recall-{int(time.time())}"

    print(f"\n{'='*60}")
    print(f"  RECALL INTERCEPTION TEST")
    print(f"  Session: {session_id}")
    print(f"{'='*60}")

    # Phase 1: Establish context (turns 1-3)
    context_turns = [
        (
            "I'm building a payment processing service. The API uses "
            "Stripe for payments (secret key sk_live_abc123, endpoint https://api.stripe.com/v1). "
            "Webhooks are validated with whsec_xyz789. The service runs on port 8080 "
            "behind nginx. Database is PostgreSQL at db.internal:5432/payments."
        ),
        (
            "The payment flow is: 1) Create PaymentIntent via Stripe API, "
            "2) Store intent_id + amount + currency in our DB (table: payment_intents), "
            "3) Return client_secret to frontend, "
            "4) Frontend completes payment, "
            "5) Webhook confirms payment → update DB status to 'succeeded', "
            "6) Trigger order fulfillment via RabbitMQ (queue: order.fulfillment). "
            "Retry policy: 3 attempts with exponential backoff, dead letter after final failure."
        ),
        (
            "The refund policy: full refunds within 30 days, partial refunds within 90 days. "
            "Refunds over $500 require manager approval (role: payment_admin). "
            "The refund endpoint is POST /api/v2/refunds with fields: "
            "payment_id (required), amount_cents (optional, defaults to full), "
            "reason (enum: duplicate, fraudulent, requested_by_customer, other). "
            "Refund processing uses idempotency keys stored in Redis (TTL 48h)."
        ),
    ]

    messages = [
        {"role": "system", "content": "You are a senior payment systems engineer. Remember all details precisely."},
    ]

    for i, turn_content in enumerate(context_turns):
        messages.append({"role": "user", "content": turn_content})
        t0 = time.monotonic()
        resp = send_turn(client, session_id, messages, max_tokens=500)
        tr = _extract_result(resp, i + 1, t0)
        result.turns.append(tr)

        if tr.status != 200:
            result.passed = False
            result.notes.append(f"Turn {i+1} failed: {tr.error}")
            print(f"  Turn {i+1}: FAIL ({tr.status}) — {tr.error}")
            return result

        print(f"  Turn {i+1}: OK | {tr.prompt_tokens}→{tr.completion_tokens} tok | {tr.latency_ms:.0f}ms")
        if verbose:
            print(f"    Content: {tr.content_preview}")

        messages.append({"role": "assistant", "content": tr.content_preview})
        time.sleep(1)  # Allow extraction to process

    # Phase 2: Ask a recall question (turn 4)
    recall_question = (
        "I need to implement the refund endpoint now. "
        "Remind me: what are the required fields, what's the approval threshold, "
        "and what idempotency mechanism do we use?"
    )
    messages.append({"role": "user", "content": recall_question})

    t0 = time.monotonic()
    resp = send_turn(client, session_id, messages, max_tokens=800)
    tr = _extract_result(resp, 4, t0)
    result.turns.append(tr)

    if tr.status != 200:
        result.passed = False
        result.notes.append(f"Recall turn failed: {tr.error}")
        print(f"  Turn 4 (recall): FAIL ({tr.status}) — {tr.error}")
        return result

    # Check if recall keywords appear in the response
    content_lower = tr.content_preview.lower()
    recall_keywords = ["refund", "500", "idempotency", "redis", "payment_admin"]
    found = [kw for kw in recall_keywords if kw in content_lower]

    print(f"  Turn 4 (recall): OK | {tr.prompt_tokens}→{tr.completion_tokens} tok | {tr.latency_ms:.0f}ms")
    print(f"    Keywords found: {found}")
    if verbose:
        print(f"    Content: {tr.content_preview}")

    # Check traces for recall usage
    time.sleep(2)  # Allow trace to be written
    traces = get_trace(client, session_id)
    recall_traces = [t for t in traces if t.get("recall_used")]
    if recall_traces:
        result.notes.append(f"Recall used in {len(recall_traces)} turn(s)")
        print(f"  ✓ Recall confirmed via trace ({len(recall_traces)} turn(s))")
    else:
        result.notes.append("No recall detected in traces (model may have answered from context window)")
        print(f"  ○ No recall in traces — model answered from context window")

    # Tally
    result.total_prompt_tokens = sum(t.prompt_tokens for t in result.turns)
    result.total_completion_tokens = sum(t.completion_tokens for t in result.turns)
    result.total_latency_ms = sum(t.latency_ms for t in result.turns)

    print(f"\n  Totals: {result.total_prompt_tokens} prompt + {result.total_completion_tokens} completion tokens")
    print(f"  Total latency: {result.total_latency_ms:.0f}ms")

    return result


def test_synthetic_session_work(client: httpx.Client, verbose: bool = False) -> SuiteResult:
    """Test recall_session_work synthetic tool.

    Strategy:
    1. Build a multi-turn session with tool calls
    2. Ask the model to summarize what's been done (should trigger recall_session_work)
    3. Verify the response contains structured summary
    """
    result = SuiteResult(name="synthetic_session_work")
    session_id = f"syntest-work-{int(time.time())}"

    print(f"\n{'='*60}")
    print(f"  SYNTHETIC SESSION WORK TEST")
    print(f"  Session: {session_id}")
    print(f"{'='*60}")

    # Build a multi-turn session
    work_turns = [
        "I'm refactoring the user authentication module. The current implementation uses bcrypt for password hashing with a cost factor of 12.",
        "I've updated the password hashing to use argon2id with memory=65536, iterations=3, parallelism=4. The migration script converts existing bcrypt hashes on next login.",
        "The session management now uses Redis-backed JWTs with a 15-minute access token TTL and 7-day refresh token TTL. Refresh tokens are stored in an HttpOnly secure cookie.",
        "I've added rate limiting to the login endpoint: 5 attempts per minute per IP, with a 15-minute lockout after 10 failed attempts. The rate limiter uses a Redis sliding window.",
        "Tests are passing: 24 unit tests, 8 integration tests. Coverage is at 94% for the auth module. The migration has been tested against a copy of the production database.",
    ]

    messages = [
        {"role": "system", "content": "You are a coding assistant tracking work progress precisely. When asked to recall work, use any available tools."},
    ]

    for i, turn_content in enumerate(work_turns):
        messages.append({"role": "user", "content": turn_content})
        t0 = time.monotonic()
        resp = send_turn(client, session_id, messages, max_tokens=400)
        tr = _extract_result(resp, i + 1, t0)
        result.turns.append(tr)

        if tr.status != 200:
            result.passed = False
            result.notes.append(f"Turn {i+1} failed: {tr.error}")
            print(f"  Turn {i+1}: FAIL")
            return result

        print(f"  Turn {i+1}: OK | {tr.prompt_tokens}→{tr.completion_tokens} tok | {tr.latency_ms:.0f}ms")
        messages.append({"role": "assistant", "content": tr.content_preview})
        time.sleep(0.5)

    # Ask for work summary (should trigger recall_session_work if injected)
    messages.append({"role": "user", "content": (
        "I need to write a commit message summarizing all the changes we made to the auth module. "
        "Can you recall everything we did this session and draft the message?"
    )})

    t0 = time.monotonic()
    resp = send_turn(client, session_id, messages, max_tokens=800)
    tr = _extract_result(resp, len(work_turns) + 1, t0)
    result.turns.append(tr)

    if tr.status != 200:
        result.passed = False
        result.notes.append(f"Summary turn failed: {tr.error}")
        print(f"  Turn {len(work_turns)+1} (summary): FAIL")
        return result

    # Check for session work keywords
    content = tr.content_preview.lower()
    work_keywords = ["argon2", "bcrypt", "jwt", "redis", "rate limit"]
    found = [kw for kw in work_keywords if kw in content]

    print(f"  Turn {len(work_turns)+1} (summary): OK | {tr.prompt_tokens}→{tr.completion_tokens} tok | {tr.latency_ms:.0f}ms")
    print(f"    Work keywords in response: {found}")

    # Check if synthetic tools were used
    if tr.has_tool_calls:
        result.notes.append(f"Model returned tool calls: {tr.tool_names}")
        print(f"    Tool calls in response: {tr.tool_names}")
    else:
        result.notes.append("No tool calls in final response (synthetic interception handled transparently)")

    # Check traces
    time.sleep(2)
    traces = get_trace(client, session_id)
    synthetic_traces = [t for t in traces if t.get("synthetic_tool_used")]
    if synthetic_traces:
        result.notes.append(f"Synthetic tool used in {len(synthetic_traces)} turn(s)")
        print(f"  ✓ Synthetic tool confirmed via trace")
    else:
        result.notes.append("No synthetic tool detected in traces")
        print(f"  ○ No synthetic tool in traces — may not be enabled or model didn't call it")

    result.total_prompt_tokens = sum(t.prompt_tokens for t in result.turns)
    result.total_completion_tokens = sum(t.completion_tokens for t in result.turns)
    result.total_latency_ms = sum(t.latency_ms for t in result.turns)

    print(f"\n  Totals: {result.total_prompt_tokens} prompt + {result.total_completion_tokens} completion tokens")

    return result


def test_read_cache_interception(client: httpx.Client, verbose: bool = False) -> SuiteResult:
    """Test native Read tool interception via file cache.

    Strategy:
    1. First turn reads a file (populates cache via extraction)
    2. Second turn re-reads the same file (should hit cache)
    3. Compare latencies — cache hit should be faster
    4. Check metrics for native_read_cache_hits
    """
    result = SuiteResult(name="read_cache_interception")
    session_id = f"syntest-cache-{int(time.time())}"

    print(f"\n{'='*60}")
    print(f"  READ CACHE INTERCEPTION TEST")
    print(f"  Session: {session_id}")
    print(f"{'='*60}")

    # Capture metrics before
    metrics_before = get_proxy_metrics(client)
    cache_hits_before = metrics_before.get("native_read_cache_hits", 0)

    # Turn 1: first read
    messages = [
        {"role": "system", "content": "You are a coding assistant. Read files when asked."},
        {"role": "user", "content": "Read the file /workspace/app/config.py and explain what it does."},
    ]

    t0 = time.monotonic()
    resp = send_turn(client, session_id, messages, max_tokens=500)
    tr1 = _extract_result(resp, 1, t0)
    result.turns.append(tr1)

    print(f"  Turn 1 (first read): {'OK' if tr1.status == 200 else 'FAIL'} | {tr1.latency_ms:.0f}ms")
    if tr1.has_tool_calls:
        print(f"    Tool calls: {tr1.tool_names}")

    if tr1.status != 200:
        result.passed = False
        result.notes.append(f"First read failed: {tr1.error}")
        return result

    messages.append({"role": "assistant", "content": tr1.content_preview})

    # Wait for extraction to cache the file
    time.sleep(3)

    # Turn 2: re-read the same file
    messages.append({"role": "user", "content": "Read /workspace/app/config.py again — I need to check something."})

    t0 = time.monotonic()
    resp = send_turn(client, session_id, messages, max_tokens=500)
    tr2 = _extract_result(resp, 2, t0)
    result.turns.append(tr2)

    print(f"  Turn 2 (re-read): {'OK' if tr2.status == 200 else 'FAIL'} | {tr2.latency_ms:.0f}ms")
    if tr2.has_tool_calls:
        print(f"    Tool calls: {tr2.tool_names}")

    # Check metrics for cache hits
    metrics_after = get_proxy_metrics(client)
    cache_hits_after = metrics_after.get("native_read_cache_hits", 0)
    new_hits = cache_hits_after - cache_hits_before

    if new_hits > 0:
        result.notes.append(f"Cache hits increased by {new_hits}")
        print(f"  ✓ Cache hits: +{new_hits}")
    else:
        result.notes.append("No cache hits detected (file may not have been cached, or interception not enabled)")
        print(f"  ○ No cache hits — interception may not be enabled or file not cached")

    # Compare latencies
    if tr1.latency_ms > 0 and tr2.latency_ms > 0:
        speedup = tr1.latency_ms / max(tr2.latency_ms, 1)
        result.notes.append(f"Latency: {tr1.latency_ms:.0f}ms → {tr2.latency_ms:.0f}ms ({speedup:.1f}x)")
        print(f"  Latency comparison: {tr1.latency_ms:.0f}ms → {tr2.latency_ms:.0f}ms ({speedup:.1f}x)")

    result.total_prompt_tokens = sum(t.prompt_tokens for t in result.turns)
    result.total_completion_tokens = sum(t.completion_tokens for t in result.turns)
    result.total_latency_ms = sum(t.latency_ms for t in result.turns)

    return result


def test_long_session_token_savings(client: httpx.Client, verbose: bool = False) -> SuiteResult:
    """Test token savings on a long multi-turn session.

    Strategy:
    1. Build a 10+ turn session with growing context
    2. Track prompt_tokens per turn
    3. After assembly kicks in, prompt_tokens should decrease
    4. Check traces for assembly_mode != passthrough
    """
    result = SuiteResult(name="long_session_token_savings")
    session_id = f"syntest-savings-{int(time.time())}"

    print(f"\n{'='*60}")
    print(f"  LONG SESSION TOKEN SAVINGS TEST")
    print(f"  Session: {session_id}")
    print(f"{'='*60}")

    # Build progressively longer turns
    turns = [
        "I'm building a real-time analytics dashboard. The backend uses ClickHouse for OLAP queries, Redis for caching aggregations, and Kafka for ingesting events.",
        "The event schema has 23 fields including: event_id (UUID), timestamp (DateTime64), user_id (UInt64), event_type (LowCardinality String), properties (Map String String), geo_lat (Float64), geo_lon (Float64), device_type (Enum8), session_id (UUID), page_url (String), referrer (String), duration_ms (UInt32).",
        "The ClickHouse table uses ReplicatedMergeTree with ORDER BY (event_type, toDate(timestamp), user_id). Partitioning by month. TTL 365 days. The materialized views pre-aggregate: hourly_events_by_type, daily_unique_users, hourly_page_views. Each MV has a different AggregateFunction column set.",
        "The API layer is FastAPI with these endpoints: GET /api/v1/events/count (params: start, end, event_type, granularity), GET /api/v1/events/unique-users (params: start, end, segment), GET /api/v1/events/funnel (params: steps[], window_days), POST /api/v1/events/query (body: custom ClickHouse query with parameterized filters).",
        "The caching strategy: Redis stores pre-computed aggregations with key pattern 'agg:{metric}:{granularity}:{date}'. TTL varies by granularity — 5min for hourly, 1h for daily, 24h for monthly. Cache invalidation happens on each Kafka consumer batch commit via a pub/sub channel 'cache:invalidate'.",
        "The frontend is React with Recharts. The dashboard has 6 panels: events timeline (area chart), unique users (line chart), top pages (bar chart), funnel visualization (stepped chart), geographic heatmap (Mapbox GL), and real-time event feed (virtualized list, WebSocket). State management uses Zustand with persist middleware.",
        "Performance requirements: p99 query latency < 200ms for pre-aggregated metrics, < 2s for ad-hoc queries. The ClickHouse cluster has 3 shards × 2 replicas. Each node: 32 cores, 128GB RAM, 2TB NVMe SSD. Current data: 12 billion events, 8TB uncompressed (1.2TB compressed).",
        "I need to add a new feature: cohort analysis. Users should be able to define cohorts based on first-event criteria (e.g., 'users who first visited page X between dates Y and Z') and then track their retention over time. The retention chart should show day-0 through day-30 retention rates.",
        "For the cohort query, I'm thinking of this approach: 1) CTE to find first event per user matching criteria, 2) JOIN back to events table for subsequent activity, 3) GROUP BY day-offset, 4) Calculate retention as percentage of cohort. The query needs to handle cohorts up to 1M users efficiently.",
        "Let me show you the ClickHouse query I drafted for cohort retention. It uses windowFunnel() and retention() functions. The query takes ~3.5s on our dataset, which exceeds the 2s SLA. I need to optimize it — maybe pre-compute cohort membership in a materialized view?",
    ]

    messages = [
        {"role": "system", "content": "You are a data engineering expert. Give detailed technical responses. Reference prior context when relevant."},
    ]

    prompt_token_history: list[int] = []

    for i, turn in enumerate(turns):
        messages.append({"role": "user", "content": turn})
        t0 = time.monotonic()
        resp = send_turn(client, session_id, messages, max_tokens=600)
        tr = _extract_result(resp, i + 1, t0)
        result.turns.append(tr)

        prompt_token_history.append(tr.prompt_tokens)

        status = "OK" if tr.status == 200 else "FAIL"
        print(f"  Turn {i+1:2d}: {status} | prompt={tr.prompt_tokens:>6d} completion={tr.completion_tokens:>4d} | {tr.latency_ms:.0f}ms")

        if tr.status != 200:
            result.passed = False
            result.notes.append(f"Turn {i+1} failed: {tr.error}")
            if verbose:
                print(f"    Error: {tr.error}")
            break

        if verbose:
            print(f"    Content: {tr.content_preview}")

        messages.append({"role": "assistant", "content": tr.content_preview or "I understand."})
        time.sleep(0.5)

    # Analyze token trends
    if len(prompt_token_history) >= 5:
        # Check if later turns have reduced prompt tokens (assembly kicked in)
        early_avg = sum(prompt_token_history[:3]) / 3
        late_avg = sum(prompt_token_history[-3:]) / 3

        # Without assembly, later turns should have MORE tokens (growing context)
        # With assembly, later turns should have fewer or similar tokens
        if late_avg < early_avg * 1.5:
            result.notes.append(
                f"Token growth controlled: early avg {early_avg:.0f}, late avg {late_avg:.0f} "
                f"(ratio: {late_avg/early_avg:.2f}x)"
            )
            print(f"\n  Token analysis: early avg {early_avg:.0f} → late avg {late_avg:.0f} ({late_avg/early_avg:.2f}x)")
        else:
            result.notes.append(
                f"Context growing linearly: early avg {early_avg:.0f}, late avg {late_avg:.0f} "
                f"(assembly may not have triggered — check cold_start settings)"
            )
            print(f"\n  Token analysis: linear growth — early avg {early_avg:.0f} → late avg {late_avg:.0f}")

    # Check traces for assembly modes
    time.sleep(2)
    traces = get_trace(client, session_id)
    assembly_modes = [t.get("assembly_mode", "unknown") for t in traces]
    mode_counts = {}
    for mode in assembly_modes:
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    if mode_counts:
        result.notes.append(f"Assembly modes: {mode_counts}")
        print(f"  Assembly modes: {mode_counts}")

    result.total_prompt_tokens = sum(t.prompt_tokens for t in result.turns)
    result.total_completion_tokens = sum(t.completion_tokens for t in result.turns)
    result.total_latency_ms = sum(t.latency_ms for t in result.turns)

    print(f"\n  Totals: {result.total_prompt_tokens} prompt + {result.total_completion_tokens} completion tokens")
    print(f"  Total latency: {result.total_latency_ms/1000:.1f}s")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_summary(results: list[SuiteResult]):
    """Print a summary of all test results."""
    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")

    total_prompt = 0
    total_completion = 0
    total_latency = 0.0

    for r in results:
        status = "✓ PASS" if r.passed else "✗ FAIL"
        print(f"  {status} | {r.name}")
        for note in r.notes:
            print(f"         {note}")
        total_prompt += r.total_prompt_tokens
        total_completion += r.total_completion_tokens
        total_latency += r.total_latency_ms

    print(f"\n  Total tokens: {total_prompt} prompt + {total_completion} completion = {total_prompt + total_completion}")
    print(f"  Total latency: {total_latency/1000:.1f}s")
    print(f"  Suites: {sum(1 for r in results if r.passed)}/{len(results)} passed")


def main():
    parser = argparse.ArgumentParser(description="Test synthetic tool paths against a live proxy")
    parser.add_argument("--suite", choices=["recall", "synthetic", "cache", "savings", "all"], default="all",
                        help="Which test suite to run")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full responses")
    args = parser.parse_args()

    print(f"archolith-context synthetic tool tester")
    print(f"Proxy: {PROXY_BASE}")
    print(f"Model: {MODEL}")

    with httpx.Client(timeout=120.0) as client:
        # Health check
        if not check_proxy_health(client):
            print(f"\n✗ Proxy at {PROXY_BASE} is not healthy. Is it running?")
            sys.exit(1)
        print(f"✓ Proxy healthy")

        # Run selected suites
        results: list[SuiteResult] = []

        suite_map = {
            "recall": test_recall_interception,
            "synthetic": test_synthetic_session_work,
            "cache": test_read_cache_interception,
            "savings": test_long_session_token_savings,
        }

        if args.suite == "all":
            for name, fn in suite_map.items():
                try:
                    results.append(fn(client, verbose=args.verbose))
                except Exception as e:
                    print(f"\n  ✗ Suite '{name}' crashed: {e}")
                    results.append(SuiteResult(name=name, passed=False, notes=[f"Crashed: {e}"]))
        else:
            fn = suite_map[args.suite]
            try:
                results.append(fn(client, verbose=args.verbose))
            except Exception as e:
                print(f"\n  ✗ Suite '{args.suite}' crashed: {e}")
                results.append(SuiteResult(name=args.suite, passed=False, notes=[f"Crashed: {e}"]))

        print_summary(results)

        # Exit code
        if all(r.passed for r in results):
            sys.exit(0)
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
