"""Test end-to-end extraction through the proxy with explicit logging."""

import asyncio
import httpx
import json


async def test():
    """Send a request to the proxy, then verify facts appear in Neo4j."""
    # Step 1: Send request through proxy
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        payload = {
            "model": "google/gemma-3-4b-it",
            "messages": [
                {"role": "system", "content": "You are a coding assistant."},
                {"role": "user", "content": "Create a Python function called add_numbers that takes two arguments and returns their sum."},
            ],
            "stream": False,
        }

        headers = {
            "Content-Type": "application/json",
            "X-Session-ID": "e2e-direct-001",
        }

        print("Sending request to proxy...")
        resp = await client.post(
            "http://localhost:9800/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        print(f"Proxy response: {resp.status_code}")
        data = resp.json()
        if "choices" in data:
            content = data["choices"][0]["message"]["content"]
            print(f"Assistant response (first 200 chars): {content[:200]}")
        else:
            print(f"Unexpected response: {json.dumps(data, indent=2)[:500]}")
            return

    # Step 2: Wait for async extraction
    print("Waiting 15s for async extraction...")
    await asyncio.sleep(15)

    # Step 3: Check Neo4j directly
    from archolith_proxy.graph.driver import init_driver, close_driver

    driver = await init_driver()

    # Check session
    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (s:ContextSession) WHERE s.session_id = $sid RETURN s.session_id, s.turn_number, s.status",
            {"sid": "e2e-direct-001"},
        )
        records = await result.data()
        print(f"\nSession: {records}")

    # Check facts
    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (f:ContextSession:Fact) WHERE f.session_id = $sid RETURN f.content, f.fact_type, f.confidence",
            {"sid": "e2e-direct-001"},
        )
        records = await result.data()
        print(f"Facts: {records}")

    # Check ALL ContextSession nodes
    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (n:ContextSession) RETURN labels(n), n.session_id, n.fact_type LIMIT 20"
        )
        records = await result.data()
        print(f"\nAll ContextSession nodes: {records}")

    await close_driver()


if __name__ == "__main__":
    asyncio.run(test())
