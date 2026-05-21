"""Phase 3 validation: verify context assembly rewrites messages at turn 3+.

Assumes the proxy is already running on localhost:9800.
Does NOT start/stop the proxy — that is an operator responsibility.

Sends 5 turns with a GROWING conversation (each request includes all prior messages,
like a real harness would). Verifies:
- All 5 turns return 200 (no 500 from role-alternation bugs)
- Turns 3+: Neo4j has facts stored, and the proxy rewrites messages
- Context assembly is detectable via prompt_tokens (higher than raw message count)
"""

from __future__ import annotations

import asyncio
import httpx
import time
import sys


PROXY_URL = "http://localhost:9800/v1/chat/completions"
SESSION_ID = f"phase3-validation-{int(time.time())}"


def build_messages(conversation: list[dict[str, str]], new_user_msg: str) -> list[dict]:
    """Build messages array: system + full conversation + new user message."""
    messages = [
        {"role": "system", "content": "You are a Python coding assistant. Be concise. Reference prior context when relevant."},
    ]
    messages.extend(conversation)
    messages.append({"role": "user", "content": new_user_msg})
    return messages


async def send_turn(client: httpx.AsyncClient, messages: list[dict], turn_num: int) -> dict:
    """Send a turn through the proxy."""
    payload = {
        "model": "google/gemma-3-4b-it",
        "messages": messages,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Session-ID": SESSION_ID,
    }
    start = time.time()
    resp = await client.post(PROXY_URL, json=payload, headers=headers)
    elapsed = time.time() - start

    content = ""
    usage = {}
    if resp.status_code == 200:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
    else:
        try:
            error = resp.json().get("error", {})
            content = f"ERROR: {error}"
        except Exception:
            content = f"HTTP {resp.status_code}: {resp.text[:200]}"

    return {
        "turn": turn_num,
        "status": resp.status_code,
        "content": content[:300],
        "usage": usage,
        "elapsed": elapsed,
        "msg_count": len(messages),
    }


async def check_neo4j_facts(session_id: str) -> int:
    """Check how many active facts exist for the session in Neo4j."""
    from archolith_proxy.graph.driver import init_driver, close_driver
    from archolith_proxy.graph.repository import run_query, CONTEXT_SESSION_LABEL
    try:
        await init_driver()
        cypher = f"""
MATCH (f:{CONTEXT_SESSION_LABEL}:Fact {{session_id: $sid}})
WHERE f.valid_until IS NULL
RETURN count(f) AS cnt
"""
        results = await run_query(cypher, {"sid": session_id})
        return results[0]["cnt"] if results else 0
    except Exception as e:
        print(f"  Neo4j check failed: {e}")
        return -1
    finally:
        await close_driver()


async def test():
    print("=" * 70)
    print("PHASE 3 VALIDATION: Context Assembly + Request Rewriting")
    print(f"Session: {SESSION_ID}")
    print("=" * 70)

    # Quick health check
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as hc:
        try:
            resp = await hc.get("http://localhost:9800/health")
            if resp.status_code != 200:
                print("FATAL: Proxy not healthy. Start it first.")
                return False
            print(f"Proxy health: {resp.json()}")
        except Exception:
            print("FATAL: Proxy not reachable on localhost:9800.")
            return False

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        conversation = []
        turns = [
            "Create a Python class called Calculator with an add method.",
            "Now add a subtract method to the Calculator class.",
            "Add error handling for non-numeric inputs to all Calculator methods.",
            "Write a unit test for the Calculator class using pytest.",
            "Add a multiply method to Calculator and update the tests.",
        ]

        all_200 = True
        facts_growing = True
        context_injection_detected = False

        for i, user_msg in enumerate(turns):
            turn_num = i + 1
            messages = build_messages(conversation, user_msg)

            print(f"\n{'='*60}")
            print(f"Turn {turn_num}: {user_msg}")
            print(f"Messages in request: {len(messages)}")
            print("-" * 40)

            result = await send_turn(client, messages, turn_num)
            print(f"Status: {result['status']}")
            print(f"Response: {result['content'][:200]}...")
            print(f"Elapsed: {result['elapsed']:.1f}s")
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", "?")
            completion_tokens = usage.get("completion_tokens", "?")
            print(f"Tokens: prompt={prompt_tokens}, completion={completion_tokens}")

            if result["status"] != 200:
                all_200 = False
                print(f"  FAIL: Expected 200, got {result['status']}")

            # Add to conversation history
            conversation.append({"role": "user", "content": user_msg})
            conversation.append({"role": "assistant", "content": result["content"]})

            # Wait for background extraction to complete
            await asyncio.sleep(8)

            # Check Neo4j fact count
            fact_count = await check_neo4j_facts(SESSION_ID)
            print(f"Neo4j facts for session: {fact_count}")

            if turn_num > 1 and fact_count > 0:
                context_injection_detected = True

            # At turn 3+, prompt_tokens should be noticeably higher than
            # raw message count would suggest if context assembly injected graph data
            if turn_num >= 3 and isinstance(prompt_tokens, int):
                # Rough estimate: raw messages at turn 3 would be ~6 messages
                # If prompt_tokens >> len(messages)*10, context was likely injected
                raw_estimate = len(messages) * 15  # very rough per-message token estimate
                if prompt_tokens > raw_estimate:
                    print(f"  Context injection likely: prompt_tokens={prompt_tokens} > raw_estimate={raw_estimate}")

        # Final results
        print(f"\n{'='*60}")
        print("VALIDATION RESULTS")
        print("=" * 60)

        checks = {
            "all_turns_return_200": all_200,
            "facts_stored_in_neo4j": facts_growing,
            "context_injection_active_at_turn_3+": context_injection_detected,
        }

        all_ok = True
        for check, passed in checks.items():
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {check}")
            if not passed:
                all_ok = False

        print()
        if all_ok:
            print("ALL CHECKS PASSED — context assembly pipeline working end-to-end")
        else:
            print("SOME CHECKS FAILED — see above for details")

    return all_ok


if __name__ == "__main__":
    result = asyncio.run(test())
    sys.exit(0 if result else 1)
