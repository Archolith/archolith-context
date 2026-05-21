"""Multi-turn e2e test through the proxy — verify session continuity + fact accumulation."""

import asyncio
import httpx
import time


async def check_neo4j(session_id: str) -> dict:
    """Check Neo4j for session + facts."""
    from archolith_proxy.graph.driver import get_driver
    driver = await get_driver()

    result_data = {}

    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (s:ContextSession:Session {session_id: $sid}) RETURN s.session_id, s.turn_number, s.status",
            {"sid": session_id},
        )
        records = await result.data()
        result_data["session"] = records

    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (f:ContextSession:Fact {session_id: $sid}) RETURN f.content, f.fact_type, f.confidence, f.source_turn ORDER BY f.source_turn",
            {"sid": session_id},
        )
        records = await result.data()
        result_data["facts"] = records

    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (d:ContextSession:Decision {session_id: $sid}) RETURN d.summary, d.turn",
            {"sid": session_id},
        )
        records = await result.data()
        result_data["decisions"] = records

    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (f:ContextSession:File {session_id: $sid}) RETURN f.path, f.status",
            {"sid": session_id},
        )
        records = await result.data()
        result_data["files"] = records

    return result_data


async def send_turn(client: httpx.AsyncClient, session_id: str, user_msg: str) -> str:
    """Send a turn through the proxy and return assistant response."""
    payload = {
        "model": "google/gemma-3-4b-it",
        "messages": [
            {"role": "system", "content": "You are a Python coding assistant. Be concise."},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Session-ID": session_id,
    }
    resp = await client.post(
        "http://localhost:9800/v1/chat/completions",
        json=payload,
        headers=headers,
    )
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return content


async def test():
    from archolith_proxy.graph.driver import init_driver, ensure_indexes, close_driver
    await init_driver()
    await ensure_indexes()

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        session_id = f"multi-turn-{int(time.time())}"

        turns = [
            "Create a Python class called Calculator with an add method.",
            "Now add a subtract method to the Calculator class.",
            "Add error handling for non-numeric inputs to all methods.",
            "Write a unit test for the Calculator class using pytest.",
            "Add a multiply method and update the tests.",
        ]

        for i, user_msg in enumerate(turns):
            print(f"\n{'='*60}")
            print(f"Turn {i+1}: {user_msg}")
            print("-" * 40)

            response = await send_turn(client, session_id, user_msg)
            print(f"Response (first 150 chars): {response[:150]}...")

            # Wait for background extraction
            await asyncio.sleep(10)

            # Check Neo4j state
            state = await check_neo4j(session_id)
            session_info = state.get("session", [])
            facts = state.get("facts", [])
            decisions = state.get("decisions", [])
            files = state.get("files", [])

            turn_num = session_info[0].get("s.turn_number", "?") if session_info else "?"
            print(f"\nSession turn_number: {turn_num}")
            print(f"Total facts: {len(facts)}")
            print(f"Total decisions: {len(decisions)}")
            print(f"Total files: {len(files)}")

            if facts:
                print("Recent facts:")
                for f in facts[-3:]:
                    print(f"  [{f['f.source_turn']}] {f['f.fact_type']}: {f['f.content'][:80]} (conf={f['f.confidence']})")

    await close_driver()
    print("\n" + "=" * 60)
    print("Multi-turn test complete!")


if __name__ == "__main__":
    asyncio.run(test())
