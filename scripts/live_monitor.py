#!/usr/bin/env python3
"""Terminal client for the archolith-proxy live stream WebSocket.

Connects to ws://localhost:<port>/ws/stream and displays proxy activity
events in real-time with color-coded output.

Usage:
    python scripts/live_monitor.py [--port 9800] [--filter request,assembly]

Requires: websockets  (pip install websockets)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


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


_dotenv = _load_dotenv(Path(__file__).parent.parent / ".env")
_DEFAULT_PORT = int(_dotenv.get("PROXY_PORT", "9800"))

try:
    import websockets
except ImportError:
    print("ERROR: websockets package required. Install with: pip install websockets")
    sys.exit(1)

# ANSI color codes
COLORS = {
    "request": "\033[36m",      # cyan
    "assembly": "\033[35m",     # magenta
    "response": "\033[32m",     # green
    "extraction": "\033[33m",   # yellow
    "session": "\033[34m",      # blue
    "recall": "\033[95m",       # bright magenta
    "dropped": "\033[31m",      # red
    "default": "\033[0m",       # reset
}

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
RED = "\033[31m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


class MonitorState:
    """Track per-session state for delta display and event folding."""

    def __init__(self) -> None:
        # session_id -> last assembly latency
        self.last_assembly_ms: dict[str, float] = {}
        # session_id -> list of recent response latencies (for average)
        self.response_latencies: dict[str, list[float]] = {}
        # event_type -> count of folded events since last non-folded event
        self.folded_counts: dict[str, int] = {}

    def record_assembly(self, session_id: str | None, latency_ms: float) -> float | None:
        """Return delta vs previous assembly latency, or None if first."""
        sid = session_id or ""
        prev = self.last_assembly_ms.get(sid)
        self.last_assembly_ms[sid] = latency_ms
        return (latency_ms - prev) if prev is not None else None

    def record_response_latency(self, session_id: str | None, latency_ms: float) -> tuple[float | None, float]:
        """Return (delta vs average, current average) for this session."""
        sid = session_id or ""
        history = self.response_latencies.setdefault(sid, [])
        history.append(latency_ms)
        # Keep last 10
        if len(history) > 10:
            history.pop(0)
        avg = sum(history) / len(history)
        delta = latency_ms - avg if len(history) > 1 else None
        return delta, avg

    def add_folded(self, evt_type: str) -> int:
        """Increment folded count for an event type, return new count."""
        self.folded_counts[evt_type] = self.folded_counts.get(evt_type, 0) + 1
        return self.folded_counts[evt_type]

    def flush_folded(self) -> dict[str, int]:
        """Return and reset all folded counts."""
        result = dict(self.folded_counts)
        self.folded_counts.clear()
        return result


def format_event(event: dict, verbose: bool = False, state: MonitorState | None = None) -> str:
    """Format a live stream event for terminal display."""
    evt_type = event.get("type", "unknown")
    ts = event.get("ts", 0)
    color = COLORS.get(evt_type, COLORS["default"])

    # Timestamp
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        time_str = dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except (OSError, ValueError):
        time_str = "???:???"

    # Session ID (truncated)
    sid = event.get("session_id")
    sid_str = sid[:8] + ".." if sid and len(sid) > 10 else (sid or "—")

    # Turn number
    turn = event.get("turn", "")

    # Event-specific details
    details = ""
    if evt_type == "request":
        model = event.get("model", "?")
        msgs = event.get("messages", "?")
        stream = "SSE" if event.get("stream") else "API"
        tok = event.get("input_tokens", 0)
        details = f"{model} {stream} msgs={msgs} tok={tok}"
    elif evt_type == "assembly":
        mode = event.get("mode", "?")
        facts = event.get("facts_injected", 0)
        savings = event.get("token_savings", 0)
        latency = event.get("latency_ms", 0)
        savings_ratio = event.get("savings_ratio", 0)
        ratio_str = f" ({savings_ratio:.0%})" if savings_ratio else ""
        # Delta vs previous assembly
        delta_str = ""
        if state:
            delta = state.record_assembly(sid, latency)
            if delta is not None:
                arrow = "↑" if delta > 0 else "↓" if delta < 0 else "="
                delta_str = f" {arrow}{abs(delta):.0f}ms"
        details = f"mode={mode} facts={facts} saved={savings}tok{ratio_str} {latency:.0f}ms{delta_str}"
    elif evt_type == "response":
        status = event.get("status", "?")
        latency = event.get("latency_ms", 0)
        otok = event.get("output_tokens")
        tok_str = f" out={otok}" if otok else ""
        # Delta vs average
        avg_str = ""
        if state:
            delta, avg = state.record_response_latency(sid, latency)
            if delta is not None and abs(delta) > 1:
                sign = "+" if delta > 0 else ""
                avg_str = f" (avg={avg:.0f}ms {sign}{delta:.0f})"
        details = f"HTTP {status} {latency:.0f}ms{tok_str}{avg_str}"
    elif evt_type == "extraction":
        facts = event.get("facts_stored", 0)
        goal = event.get("session_goal")
        latency = event.get("latency_ms", 0)
        goal_str = f' goal="{goal}"' if goal else ""
        details = f"facts={facts} {latency:.0f}ms{goal_str}"
    elif evt_type == "session":
        sevt = event.get("event", "?")
        goal = event.get("goal")
        goal_str = f' goal="{goal}"' if goal else ""
        details = f"{sevt}{goal_str}"
    elif evt_type == "recall":
        q = event.get("question", "?")
        facts = event.get("facts_returned", 0)
        details = f'q="{q[:40]}" facts={facts}'
    elif evt_type == "dropped":
        reason = event.get("reason", "unknown")
        details = f"reason={reason}"
    elif verbose:
        # Fallback: dump the whole event
        details = json.dumps({k: v for k, v in event.items() if k not in ("type", "ts")}, default=str)

    turn_str = f"t{turn}" if turn else ""
    prefix = f"{color}{BOLD}[{evt_type.upper()}]{RESET}"
    return f"{DIM}{time_str}{RESET} {prefix} {color}{sid_str}{RESET} {turn_str} {details}"


def format_detail_line(event: dict) -> str | None:
    """Produce a detail/trace sub-line for an event, or None."""
    evt_type = event.get("type", "")
    sid = event.get("session_id")
    turn = event.get("turn")

    if evt_type == "assembly":
        mode = event.get("mode", "?")
        latency = event.get("latency_ms", 0)
        savings_ratio = event.get("savings_ratio", 0)
        ratio_str = f" | savings={savings_ratio:.0%}" if savings_ratio else ""
        reason = event.get("reason", "")
        reason_str = ""
        if mode == "fallback" and reason:
            reason_str = f' | {RED}reason="{reason}"{RESET}'
        savings_tok = event.get("token_savings", 0)
        return f"  └─ mode={mode}{ratio_str}{reason_str} | {savings_tok}tok | {latency:.0f}ms"

    # Trace link for any event with session_id and turn
    if sid and turn:
        return f"  └─ {DIM}trace: /trace/sessions/{sid}/turns?turn={turn}{RESET}"

    return None


async def monitor(
    uri: str,
    event_filter: set[str] | None,
    verbose: bool,
    session_filter: str | None = None,
    fold_types: set[str] | None = None,
) -> None:
    """Connect to the WebSocket and print events."""
    state = MonitorState()
    print(f"Connecting to {uri} ...")
    try:
        async with websockets.connect(uri) as ws:
            print(f"{COLORS['response']}Connected.{RESET} Listening for events (Ctrl+C to quit)\n")
            async for raw_msg in ws:
                try:
                    event = json.loads(raw_msg)
                except json.JSONDecodeError:
                    print(f"{COLORS['dropped']}INVALID JSON: {raw_msg[:100]}{RESET}")
                    continue

                evt_type = event.get("type", "")

                # Session filter
                if session_filter:
                    sid = event.get("session_id", "")
                    if not sid.startswith(session_filter):
                        continue

                # Event type filter
                if event_filter and evt_type not in event_filter:
                    continue

                # Folded event handling
                if fold_types and evt_type in fold_types:
                    count = state.add_folded(evt_type)
                    if count % 10 == 0:  # Show periodic summary
                        print(f"{DIM}  ... {evt_type} events: {count} folded{RESET}")
                    continue

                # Flush any folded event counts before showing this event
                if fold_types:
                    flushed = state.flush_folded()
                    for folded_type, count in flushed.items():
                        color = COLORS.get(folded_type, COLORS["default"])
                        print(f"  {color}{DIM}[{folded_type.upper()}] {count} events folded{RESET}")

                line = format_event(event, verbose=verbose, state=state)
                print(line)

                # Detail sub-line
                detail = format_detail_line(event)
                if detail:
                    print(detail)

                if evt_type == "dropped":
                    print(f"{COLORS['dropped']}Server disconnected us (queue overflow). Reconnecting...{RESET}")
                    break  # Exit inner loop; outer loop will reconnect
    except websockets.ConnectionClosed as e:
        print(f"\n{COLORS['dropped']}Connection closed: {e.code} {e.reason}{RESET}")
    except OSError as e:
        print(f"\n{COLORS['dropped']}Connection error: {e}{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Terminal monitor for archolith-proxy live stream",
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT,
        help=f"Proxy port (default: {_DEFAULT_PORT}, from .env PROXY_PORT)",
    )
    parser.add_argument(
        "--host", default="localhost",
        help="Proxy host (default: localhost)",
    )
    parser.add_argument(
        "--filter",
        dest="event_filter",
        default=None,
        help="Comma-separated event types to show (e.g. request,assembly,response)",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Filter to events matching a session_id prefix",
    )
    parser.add_argument(
        "--fold",
        dest="fold_types",
        default=None,
        help="Comma-separated event types to fold/collapse (e.g. session)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show full event data for unknown event types",
    )
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help="Auto-reconnect on disconnect",
    )
    args = parser.parse_args()

    uri = f"ws://{args.host}:{args.port}/ws/stream"
    event_filter = set(args.event_filter.split(",")) if args.event_filter else None
    fold_types = set(args.fold_types.split(",")) if args.fold_types else None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            loop.run_until_complete(
                monitor(uri, event_filter, args.verbose, args.session, fold_types)
            )
        except KeyboardInterrupt:
            print(f"\n{RESET}Stopped.")
            break

        if not args.reconnect:
            break

        print(f"\n{COLORS['assembly']}Reconnecting in 3s...{RESET}")
        try:
            loop.run_until_complete(asyncio.sleep(3))
        except KeyboardInterrupt:
            print(f"\n{RESET}Stopped.")
            break

    loop.close()


if __name__ == "__main__":
    main()
