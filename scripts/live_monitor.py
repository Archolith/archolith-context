#!/usr/bin/env python3
"""Terminal client for the context-engine live stream WebSocket.

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


def format_event(event: dict, verbose: bool = False) -> str:
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
        details = f"mode={mode} facts={facts} saved={savings}tok {latency}ms"
    elif evt_type == "response":
        status = event.get("status", "?")
        latency = event.get("latency_ms", 0)
        otok = event.get("output_tokens")
        tok_str = f" out={otok}" if otok else ""
        details = f"HTTP {status} {latency}ms{tok_str}"
    elif evt_type == "extraction":
        facts = event.get("facts_stored", 0)
        goal = event.get("session_goal")
        latency = event.get("latency_ms", 0)
        goal_str = f' goal="{goal}"' if goal else ""
        details = f"facts={facts} {latency}ms{goal_str}"
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


async def monitor(uri: str, event_filter: set[str] | None, verbose: bool) -> None:
    """Connect to the WebSocket and print events."""
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
                if event_filter and evt_type not in event_filter:
                    continue

                line = format_event(event, verbose=verbose)
                print(line)

                if evt_type == "dropped":
                    print(f"{COLORS['dropped']}Server disconnected us (queue overflow). Reconnecting...{RESET}")
                    break  # Exit inner loop; outer loop will reconnect
    except websockets.ConnectionClosed as e:
        print(f"\n{COLORS['dropped']}Connection closed: {e.code} {e.reason}{RESET}")
    except OSError as e:
        print(f"\n{COLORS['dropped']}Connection error: {e}{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Terminal monitor for context-engine live stream",
    )
    parser.add_argument(
        "--port", type=int, default=9800,
        help="Proxy port (default: 9800)",
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

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
        try:
            loop.run_until_complete(monitor(uri, event_filter, args.verbose))
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
