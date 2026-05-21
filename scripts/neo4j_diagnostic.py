"""Quick Neo4j diagnostic for E2E session data."""

import asyncio
from archolith_proxy.graph.driver import init_driver, close_driver
from archolith_proxy.graph.repository import run_query


async def check():
    await init_driver()
    try:
        # List sessions
        sessions = await run_query(
            "MATCH (s:ContextSession:Session) "
            "RETURN s.session_id AS id, s.turn_number AS turn, s.status AS status "
            "LIMIT 10",
            {},
        )
        print(f"Sessions: {sessions}")

        # Check facts for test session
        facts = await run_query(
            "MATCH (f:ContextSession:Fact {session_id: $sid}) "
            "WHERE f.valid_until IS NULL "
            "RETURN count(f) AS cnt",
            {"sid": "e2e-test-session-002"},
        )
        print(f"Active facts for e2e-test-session-002: {facts}")

        # Check all facts
        all_facts = await run_query(
            "MATCH (f:ContextSession:Fact) "
            "RETURN f.session_id AS sid, f.content AS content, f.fact_type AS type "
            "LIMIT 20",
            {},
        )
        print(f"\nAll facts (first 20):")
        for f in all_facts:
            content = f.get("content", "?")[:80]
            print(f"  [{f.get('sid','?')[:20]}] {f.get('type','?')}: {content}")

    finally:
        await close_driver()


if __name__ == "__main__":
    asyncio.run(check())
