#!/usr/bin/env python3
"""Proxy restart script with LadybugDB health check.

Kills any existing proxy on port 9801, checks the LadybugDB file for signs
of WAL corruption from a force-kill, and starts a fresh proxy. Falls back to
a timestamped fresh DB if the existing one fails graph initialization.

Usage:
    python scripts/proxy_restart.py [--port 9801] [--fresh] [--db PATH]

Options:
    --port N        Proxy port (default: 9801)
    --fresh         Always create a new timestamped DB (skip health check)
    --db PATH       Override LADYBUG_DB_PATH from .env
    --timeout N     Seconds to wait for graph_ready (default: 20)
    --no-check      Skip DB corruption check (start with existing DB as-is)

Environment:
    LADYBUG_DB_PATH   Path to LadybugDB file (read from .env if not set)
    PROXY_PORT        Proxy port (read from .env if not set)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path


# ── Config loading ────────────────────────────────────────────────────────────

def load_dotenv(path: Path) -> dict[str, str]:
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


def write_dotenv_key(path: Path, key: str, value: str) -> None:
    """Update a single key in .env without clobbering other keys."""
    if not path.exists():
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ── Process management ────────────────────────────────────────────────────────

def find_pid_on_port(port: int) -> int | None:
    """Find the PID of the process listening on a port (Windows + Unix)."""
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        return int(parts[-1])
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10
            )
            out = result.stdout.strip()
            if out:
                return int(out.splitlines()[0])
    except Exception:
        pass
    return None


def kill_pid(pid: int) -> bool:
    """Kill a process by PID."""
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, timeout=10
            )
        else:
            os.kill(pid, 9)
        return True
    except Exception:
        return False


def kill_proxy(port: int) -> bool:
    """Kill whatever is on the given port."""
    pid = find_pid_on_port(port)
    if pid and pid > 0:
        print(f"  Killing PID {pid} on port {port}...")
        if kill_pid(pid):
            time.sleep(1.5)
            return True
        else:
            print(f"  WARNING: Could not kill PID {pid}")
            return False
    return True  # Nothing was running


# ── DB health check ───────────────────────────────────────────────────────────

def check_db_health(db_path: Path) -> dict:
    """Check whether a LadybugDB file is likely healthy.

    Signs of potential corruption from a force-kill:
    - WAL file exists and is substantially larger than the base file
      (WAL > base * 2 suggests uncommitted writes that LadybugDB couldn't recover)
    - Base file is missing (WAL only — completely orphaned)

    Returns a dict with keys: healthy (bool), base_size, wal_size, reason.
    """
    wal_path = db_path.with_suffix(db_path.suffix + ".wal")

    base_exists = db_path.exists()
    wal_exists = wal_path.exists()

    base_size = db_path.stat().st_size if base_exists else 0
    wal_size = wal_path.stat().st_size if wal_exists else 0

    if not base_exists and not wal_exists:
        return {"healthy": True, "base_size": 0, "wal_size": 0, "reason": "fresh (no files yet)"}

    if not base_exists and wal_exists:
        return {
            "healthy": False,
            "base_size": 0,
            "wal_size": wal_size,
            "reason": "WAL exists but base file missing — orphaned WAL",
        }

    # Header-only base (4096 bytes) with large WAL = uncommitted writes
    if base_size <= 4096 and wal_size > 50_000:
        return {
            "healthy": False,
            "base_size": base_size,
            "wal_size": wal_size,
            "reason": f"header-only base ({base_size}B) with large WAL ({wal_size:,}B) — likely force-kill corruption",
        }

    return {"healthy": True, "base_size": base_size, "wal_size": wal_size, "reason": "ok"}


def fresh_db_path(original: Path) -> Path:
    """Generate a timestamped fresh DB path next to the original."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = original.stem.split("_")[0]  # strip _test3 etc.
    return original.parent / f"{stem}_{ts}.lbug"


# ── Proxy start ───────────────────────────────────────────────────────────────

def start_proxy(root: Path, db_path: Path, port: int) -> subprocess.Popen:
    """Start the proxy as a background process."""
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = root / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["LADYBUG_DB_PATH"] = str(db_path)

    log_out = root / "data" / "proxy_latest.log"
    log_err = root / "data" / "proxy_latest_err.log"
    log_out.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "archolith_proxy.main:app",
         "--host", "0.0.0.0", "--port", str(port)],
        env=env,
        cwd=str(root),
        stdout=open(log_out, "w"),
        stderr=open(log_err, "w"),
    )
    print(f"  Proxy started (PID {proc.pid})")
    print(f"  Logs: {log_out}")
    return proc


# ── Health polling ────────────────────────────────────────────────────────────

def wait_for_graph_ready(port: int, timeout: int = 20) -> bool:
    """Poll /metrics until graph_ready=true or timeout."""
    url = f"http://localhost:{port}/metrics"
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                data = json.loads(r.read().decode())
                if data.get("graph_ready"):
                    print(f"\n  graph_ready=true ({data.get('uptime_s', '?')}s uptime)")
                    return True
        except Exception:
            pass
        print(".", end="", flush=True)
        dots += 1
        time.sleep(1)
    print("\n  TIMEOUT: graph never became ready")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Proxy restart with DB health check")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--fresh", action="store_true", help="Always use a fresh timestamped DB")
    parser.add_argument("--db", default=None, help="Override LADYBUG_DB_PATH")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--no-check", action="store_true", help="Skip DB corruption check")
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    dotenv_path = root / ".env"
    dotenv = load_dotenv(dotenv_path)

    port = args.port or int(dotenv.get("PROXY_PORT", "9801"))

    # Resolve DB path
    db_raw = args.db or dotenv.get("LADYBUG_DB_PATH", "./data/context.lbug")
    db_path = Path(db_raw) if Path(db_raw).is_absolute() else root / db_raw

    print(f"\n{'='*55}")
    print(f"  Archolith Proxy Restart")
    print(f"{'='*55}")
    print(f"  Port:    {port}")
    print(f"  DB:      {db_path}")

    # ── Step 1: Kill existing proxy ──────────────────────────────────────────
    print("\n[1] Killing existing proxy...")
    kill_proxy(port)
    print("  Done.")

    # ── Step 2: DB health check ──────────────────────────────────────────────
    use_db = db_path

    if args.fresh:
        use_db = fresh_db_path(db_path)
        print(f"\n[2] --fresh flag: using new DB -> {use_db.name}")
    elif args.no_check:
        print(f"\n[2] --no-check: using existing DB as-is")
    else:
        print(f"\n[2] Checking DB health...")
        health = check_db_health(db_path)
        status = "[OK]  healthy" if health["healthy"] else "[!!] CORRUPTED"
        print(f"  {status}: {health['reason']}")
        print(f"  base={health['base_size']:,}B  wal={health['wal_size']:,}B")

        if not health["healthy"]:
            use_db = fresh_db_path(db_path)
            print(f"  -> Switching to fresh DB: {use_db.name}")
            # Update .env so subsequent restarts use the same DB
            write_dotenv_key(dotenv_path, "LADYBUG_DB_PATH", f"./{use_db.relative_to(root).as_posix()}")
            print(f"  -> Updated .env LADYBUG_DB_PATH")
        else:
            print(f"  -> Using existing DB")

    # ── Step 3: Start proxy ──────────────────────────────────────────────────
    print(f"\n[3] Starting proxy on port {port}...")
    start_proxy(root, use_db, port)

    # ── Step 4: Wait for graph_ready ─────────────────────────────────────────
    print(f"\n[4] Waiting for graph_ready (timeout={args.timeout}s)...", end="", flush=True)
    ready = wait_for_graph_ready(port, timeout=args.timeout)

    if not ready:
        print(f"\n  Proxy may have started without graph support.")
        print(f"  Check logs: {root / 'data' / 'proxy_latest_err.log'}")
        sys.exit(1)

    # ── Step 5: Print final status ───────────────────────────────────────────
    print(f"\n[5] Proxy ready.")
    print(f"  Run: python scripts/proxy_status.py metrics")
    print(f"  Run: python scripts/live_monitor.py")


if __name__ == "__main__":
    main()
