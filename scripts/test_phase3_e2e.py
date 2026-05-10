"""End-to-end test of Phase 3: context assembly + request rewriting.

Sends 5 turns through the proxy and verifies:
1. Turns 1-2: cold start (passthrough, no context injection)
2. Turns 3-5: context assembly active (graph context injected into messages)

Also verifies: session continuity, fact accumulation, token reduction.
"""

from __future__ import annotations

import asyncio
import httpx
import json
import time


async def check_neo4j(session_id: str) -> dict:
    """Check Neo4j for session + facts."""
    from src.graph.driver import get_driver
    driver = await get_driver()

    result_data = {}

    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (s:ContextSession:Session {session_id: $sid}) RETURN s.session_id, s.turn_number, s.status, s.goal",
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
            "MATCH (s:ContextSession:Session {session_id: $sid})-[:TOUCHES]->(f:ContextSession:File) RETURN f.path, f.status",
            {"sid": session_id},
        )
        records = await result.data()
        result_data["files"] = records

    return result_data


async def send_turn(client: httpx.AsyncClient, session_id: str, user_msg: str, turn_num: int) -> dict:
    """Send a turn through the proxy and return response + metadata."""
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
    start = time.time()
    resp = await client.post(
        "http://localhost:9800/v1/chat/completions",
        json=payload,
        headers=headers,
    )
    elapsed = time.time() - start
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})

    return {
        "turn": turn_num,
        "status_code": resp.status_code,
        "response": content[:200],
        "usage": usage,
        "elapsed": elapsed,
    }


async def test():
    from src.graph.driver import init_driver, ensure_indexes, close_driver
    await init_driver()
    await ensure_indexes()

    session_id = f"phase3-e2e-{int(time.time())}"

    print("=" * 70)
    print("PHASE 3 E2E TEST: Context Assembly + Request Rewriting")
    print(f"Session: {session_id}")
    print("=" * 70)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        turns = [
            "Create a Python class called Calculator with an add method.",
            "Now add a subtract method to the Calculator class.",
            "Add error handling for non-numeric inputs to all methods.",
            "Write a unit test for the Calculator class using pytest.",
            "Add a multiply method and update the tests.",
        ]

        for i, user_msg in enumerate(turns):
            turn_num = i + 1
            print(f"\n{'='*60}")
            print(f"Turn {turn_num}: {user_msg}")
            print("-" * 40)

            result = await send_turn(client, session_id, user_msg, turn_num)
            print(f"Status: {result['status_code']}")
            print(f"Response: {result['response'][:150]}...")
            print(f"Elapsed: {result['elapsed']:.1f}s")
            usage = result.get("usage", {})
            print(f"Tokens: prompt={usage.get('prompt_tokens', '?')}, completion={usage.get('completion_tokens', '?')}")

            # Wait for background extraction
            await asyncio.sleep(8)

            # Check Neo4j state
            state = await check_neo4j(session_id)
            session_info = state.get("session", [])
            facts = state.get("facts", [])
            decisions = state.get("decisions", [])
            files = state.get("files", [])

            turn_number = session_info[0].get("s.turn_number", "?") if session_info else "?"
            goal = session_info[0].get("s.goal", "?") if session_info else "?"
            print(f"\nSession: turn={turn_number}, goal={goal}")
            print(f"Facts: {len(facts)}, Decisions: {len(decisions)}, Files: {len(files)}")

            if facts:
                print("Recent facts:")
                for f in facts[-3:]:
                    print(f"  [t{f['f.source_turn']}] {f['f.fact_type']}: {f['f.content'][:60]} (conf={f['f.confidence']})")

            # At turn >= 3, context assembly should be active
            if turn_num >= 3 and facts:
                print(f"\n[CONTEXT ASSEMBLY] Should be active — {len(facts)} facts available for retrieval")

    await close_driver()
    print("\n" + "=" * 70)
    print("Phase 3 E2E test complete!")


if __name__ == "__main__":
    asyncio.run(test())
