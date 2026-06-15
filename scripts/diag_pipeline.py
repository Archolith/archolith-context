"""Diagnostic script — test session creation + extraction end-to-end.

Backend-aware: initializes the configured graph backend (ladybug by default)
against a FRESH temp DB, so it never touches or locks the live graph DB held by
a running proxy. graph.session uses run_query/run_write, which dispatch to the
active backend, so the session checks work for either backend.
"""

import asyncio
import os
import tempfile
from pathlib import Path

import httpx


async def test():
    from archolith_proxy.config import get_settings
    settings = get_settings()

    # 1. Initialize the configured graph backend on a fresh temp DB (never the
    #    live DB — that is locked by the running proxy).
    from archolith_proxy.graph.backend import close_backend, init_backend

    tmp_db = Path(tempfile.gettempdir()) / f"diag_pipeline_{os.getpid()}.lbug"
    if settings.graph_backend == "ladybug":
        from archolith_proxy.graph.ladybug_backend import LadybugBackend
        await init_backend(LadybugBackend(
            db_path=str(tmp_db),
            max_concurrent_queries=settings.ladybug_max_concurrent,
        ))
        print(f"1. Graph backend OK (ladybug, temp DB {tmp_db.name})")
    else:
        from archolith_proxy.graph.neo4j_backend import Neo4jBackend
        await init_backend(Neo4jBackend())
        print("1. Graph backend OK (neo4j)")

    # Use the active backend's session methods (graph.session goes through the
    # Neo4j-only run_query path, so call the backend abstraction directly).
    from archolith_proxy.graph.backend import get_backend
    backend = get_backend()
    try:
        result = await backend.create_session("test-direct-002", fingerprint="fp_test_002")
        sid = result.get("session_id", "?") if isinstance(result, dict) else "?"
        print(f"2. Session created: session_id={sid}")
    except Exception as e:
        print(f"2. Session create FAILED: {type(e).__name__}: {e}")

    # Verify
    found = await backend.find_session_by_id("test-direct-002")
    print(f"3. Session found: {found is not None}")

    # Touch + turn
    await backend.touch_session("test-direct-002")
    turn = await backend.get_turn_number("test-direct-002")
    print(f"4. Turn after touch: {turn}")

    # 2. Test extraction
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0))
    try:
        from archolith_proxy.extractor.client import extract_facts
        result = await extract_facts(
            http_client=client,
            turn_number=1,
            user_message="What is 2+2?",
            assistant_response="4",
        )
        if result:
            print(f"5. Extraction OK: facts={len(result.facts)}, files={len(result.files_touched)}, decisions={len(result.decisions)}")
            if result.facts:
                print(f"   First fact: {result.facts[0]}")
        else:
            print("5. Extraction returned None (API call failed)")
    except Exception as e:
        print(f"5. Extraction FAILED: {type(e).__name__}: {e}")
    finally:
        await client.aclose()

    await close_backend()
    # Clean up the temp DB (+ WAL).
    try:
        tmp_db.unlink(missing_ok=True)
        tmp_db.with_suffix(tmp_db.suffix + ".wal").unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(test())
