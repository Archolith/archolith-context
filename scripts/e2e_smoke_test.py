"""E2E smoke test: multi-turn through proxy with fact extraction + context assembly."""

import httpx
import json
import os
import time
import sys

from dotenv import load_dotenv
load_dotenv()

_port = os.getenv("PROXY_PORT", "9800")
_base_host = os.getenv("PROXY_HOST", f"localhost:{_port}")
BASE = os.getenv("PROXY_URL", f"http://{_base_host}/v1")
ADMIN_BASE = BASE.rsplit("/v1", 1)[0]
SESSION = "e2e-test-session-002"
HEADERS = {"Content-Type": "application/json", "X-Session-ID": SESSION}
MODEL = os.getenv("BENCHMARK_MODEL", "deepseek-chat")


def main():
    messages = []

    turns = [
        "Create a Python class called Calculator with an add method that takes two numbers.",
        "Now add a subtract method to the Calculator class.",
        "Add error handling for non-numeric inputs to all methods.",
        "Write a unit test for the Calculator class using pytest.",
        "Add a multiply method and update the tests.",
    ]

    for i, user_msg in enumerate(turns):
        messages.append({"role": "user", "content": user_msg})
        payload = {
            "model": MODEL,
            "messages": messages,
            "max_tokens": 500,
            "temperature": 0.3,
        }
        try:
            resp = httpx.post(
                f"{BASE}/chat/completions",
                json=payload,
                headers=HEADERS,
                timeout=90,
            )
            data = resp.json()
            if resp.status_code != 200:
                print(f"Turn {i+1}: ERROR {resp.status_code}")
                print(f"  Body: {json.dumps(data)[:300]}")
                break
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            p_tokens = usage.get("prompt_tokens", "?")
            c_tokens = usage.get("completion_tokens", "?")
            print(f"Turn {i+1}: OK | in={p_tokens} out={c_tokens} | {content[:80]}...")
            messages.append({"role": "assistant", "content": content})
        except Exception as e:
            print(f"Turn {i+1}: EXCEPTION {e}")
            break

        # Allow extraction to complete before next turn
        time.sleep(3)

    # Check session via trace endpoint (works with LadybugDB; /sessions/{id} requires Neo4j)
    print("\n--- Session Check ---")
    try:
        sess = httpx.get(f"{ADMIN_BASE}/trace/sessions/{SESSION}", timeout=5)
        print(f"Session status: {sess.status_code}")
        if sess.status_code == 200:
            sd = sess.json()
            summary = sd.get("summary", sd)  # /trace/sessions/{id} wraps in {"summary":..., "turns":[...]}
            print(f"  Turns:         {summary.get('turn_count', '?')}")
            print(f"  User turns:    {summary.get('max_user_turns', '?')}")
            print(f"  Modes:         {summary.get('assembly_modes', {})}")
            print(f"  Facts stored:  {summary.get('total_facts_stored', '?')}")
            print(f"  Goal:          {summary.get('goal', '(none)')}")
            turns = sd.get("turns", [])
            if turns:
                print(f"\n  Per-turn:")
                for t in turns:
                    mode = t.get("assembly_mode", "?")
                    uturn = t.get("user_turn_count", "?")
                    tokens = t.get("input_tokens", 0)
                    facts = t.get("facts_stored", 0)
                    print(f"    turn {t.get('turn_number',0):02d}  mode={mode:<12} user_turn={uturn}  in={tokens}  facts_stored={facts}")
    except Exception as e:
        print(f"Session check failed: {e}")

    # Check metrics
    print("\n--- Metrics ---")
    try:
        met = httpx.get(f"{ADMIN_BASE}/metrics", timeout=5)
        md = met.json()
        for k, v in md.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"Metrics check failed: {e}")


if __name__ == "__main__":
    main()
