"""Diagnostic script — test session creation + extraction end-to-end."""

import asyncio
import httpx


async def test():
    from archolith_proxy.config import get_settings
    settings = get_settings()

    # 1. Test Neo4j session creation
    from archolith_proxy.graph.driver import init_driver, ensure_indexes, close_driver
    driver = await init_driver()
    await ensure_indexes()
    print("1. Neo4j driver OK")

    from archolith_proxy.graph.session import create_session, find_by_session_id, touch_session, get_turn_number
    try:
        result = await create_session("test-direct-002", fingerprint="fp_test_002")
        sid = result.get("session_id", "?") if isinstance(result, dict) else "?"
        print(f"2. Session created: session_id={sid}")
    except Exception as e:
        print(f"2. Session create FAILED: {type(e).__name__}: {e}")

    # Verify
    found = await find_by_session_id("test-direct-002")
    print(f"3. Session found: {found is not None}")

    # Touch + turn
    await touch_session("test-direct-002")
    turn = await get_turn_number("test-direct-002")
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

    await close_driver()


if __name__ == "__main__":
    asyncio.run(test())
