"""Direct test of the _run_extraction function through the proxy's running components."""

import asyncio
import httpx


async def test():
    from archolith_proxy.config import get_settings
    settings = get_settings()

    # Initialize same as the proxy
    from archolith_proxy.graph.driver import init_driver, ensure_indexes, close_driver
    await init_driver()
    await ensure_indexes()

    # Create HTTP client same as proxy
    extractor_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
    )

    # Import and call _run_extraction directly
    from archolith_proxy.openai.extraction import _run_extraction

    messages = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Create a Python function called add_numbers that takes two arguments and returns their sum."},
    ]
    response_text = """```python
def add_numbers(x, y):
    \"\"\"Add two numbers together.\"\"\"
    return x + y
```"""

    print("Calling _run_extraction directly...")
    try:
        await _run_extraction(
            client=extractor_client,
            session_id="e2e-direct-001",
            turn_number=1,
            messages=messages,
            response_text=response_text,
        )
        print("Extraction completed without exception")
    except Exception as e:
        print(f"Extraction FAILED: {type(e).__name__}: {e}")

    await extractor_client.aclose()

    # Now check Neo4j
    from archolith_proxy.graph.driver import get_driver
    driver = await get_driver()
    async with driver.session(database="neo4j") as session:
        result = await session.run(
            "MATCH (f:ContextSession:Fact) WHERE f.session_id = $sid RETURN f.content, f.fact_type, f.confidence, f.source_turn",
            {"sid": "e2e-direct-001"},
        )
        records = await result.data()
        print(f"\nFacts after extraction: {records}")

    await close_driver()


if __name__ == "__main__":
    asyncio.run(test())
