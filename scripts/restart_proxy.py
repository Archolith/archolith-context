"""Gracefully restart the archolith proxy.

Usage:
    python scripts/restart_proxy.py [--port 9801] [--no-start] [--no-rotate-db]

Steps:
1. POST /admin/shutdown — triggers lifespan teardown (closes LadybugDB + WAL flush)
2. Wait for the process to exit (polls /health until 503/connection-refused)
3. Rotate LADYBUG_DB_PATH in .env to a fresh timestamped file (unless --no-rotate-db)
4. Start a new proxy via uvicorn (unless --no-start)

DO NOT use Stop-Process -Force or taskkill /F — that skips WAL flush and corrupts
the LadybugDB file. Always go through this script or /admin/shutdown directly.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime

import httpx


def _load_dotenv(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _rotate_db_path(env_path: str) -> str:
    """Rewrite LADYBUG_DB_PATH in .env to a fresh timestamped file.

    Returns the new DB path, or empty string if rotation was skipped
    (e.g. LADYBUG_DB_PATH not present or file not found).
    """
    try:
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return ""

    # Find existing LADYBUG_DB_PATH line
    match = re.search(r"^(LADYBUG_DB_PATH\s*=\s*)(.+)$", content, re.MULTILINE)
    if not match:
        return ""

    old_value = match.group(2).strip().strip('"').strip("'")

    # Derive the data directory from the current path
    if "/" in old_value or "\\" in old_value:
        data_dir = old_value.replace("\\", "/").rsplit("/", 1)[0]
    else:
        data_dir = "./data"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_db_name = f"context_{timestamp}.lbug"
    new_value = f"{data_dir}/{new_db_name}"

    new_content = content[:match.start(2)] + new_value + content[match.end(2):]
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return new_value


def main() -> None:
    parser = argparse.ArgumentParser(description="Gracefully restart the archolith proxy")
    parser.add_argument("--port", type=int, default=0, help="Proxy port (default: read from .env PROXY_PORT or 9801)")
    parser.add_argument("--no-start", action="store_true", help="Shut down but do not restart")
    parser.add_argument("--no-rotate-db", action="store_true", help="Skip rotating LADYBUG_DB_PATH to a fresh file")
    parser.add_argument("--admin-token", default="", help="Admin token (default: read from .env ADMIN_TOKEN)")
    args = parser.parse_args()

    env = _load_dotenv(".env")
    port = args.port or int(env.get("PROXY_PORT", "9801"))
    admin_token = args.admin_token or env.get("ADMIN_TOKEN", "")
    base_url = f"http://localhost:{port}"

    headers = {}
    if admin_token:
        headers["X-Admin-Token"] = admin_token

    # ── Step 1: Graceful shutdown ─────────────────────────────────────────────
    print(f"[restart_proxy] Sending shutdown to {base_url}/admin/shutdown ...", flush=True)
    try:
        r = httpx.post(f"{base_url}/admin/shutdown", headers=headers, timeout=5.0)
        if r.status_code == 200:
            print(f"[restart_proxy] Shutdown accepted: {r.json()}", flush=True)
        elif r.status_code == 401:
            print("[restart_proxy] ERROR: admin token required but not provided (set ADMIN_TOKEN in .env)", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"[restart_proxy] WARNING: unexpected status {r.status_code}: {r.text[:200]}", flush=True)
    except httpx.ConnectError:
        print(f"[restart_proxy] Proxy not running at {base_url} — skipping shutdown", flush=True)

    # ── Step 2: Wait for process to exit ─────────────────────────────────────
    print("[restart_proxy] Waiting for proxy to stop ...", flush=True)
    deadline = time.time() + 15
    stopped = False
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1.0)
            time.sleep(0.5)
        except (httpx.ConnectError, httpx.TimeoutException):
            stopped = True
            break

    if not stopped:
        print("[restart_proxy] WARNING: proxy did not stop within 15s — may still be running", flush=True)
    else:
        print("[restart_proxy] Proxy stopped cleanly.", flush=True)

    if args.no_start:
        print("[restart_proxy] --no-start set, done.", flush=True)
        return

    # ── Step 3: Rotate DB path ────────────────────────────────────────────────
    if not args.no_rotate_db:
        new_db = _rotate_db_path(".env")
        if new_db:
            print(f"[restart_proxy] Rotated LADYBUG_DB_PATH -> {new_db}", flush=True)
        else:
            print("[restart_proxy] DB rotation skipped (LADYBUG_DB_PATH not found in .env)", flush=True)

    # ── Step 4: Restart ───────────────────────────────────────────────────────
    print(f"[restart_proxy] Starting proxy on port {port} ...", flush=True)
    subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "archolith_proxy.main:app",
            "--host", "0.0.0.0",
            "--port", str(port),
        ],
        # Detach: let it run independently
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    # Wait for it to come up
    deadline = time.time() + 20
    up = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                up = True
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.5)

    if up:
        print(f"[restart_proxy] Proxy is up at {base_url} OK", flush=True)
    else:
        print(f"[restart_proxy] WARNING: proxy did not come up within 20s — check logs", flush=True)


if __name__ == "__main__":
    main()
