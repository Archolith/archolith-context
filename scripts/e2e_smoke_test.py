"""E2E smoke test: multi-turn through proxy with fact extraction + context assembly."""

import httpx
import json
import time
import sys

BASE = "http://localhost:9800/v1"
SESSION = "e2e-test-session-002"
HEADERS = {"Content-Type": "application/json", "X-Session-ID": SESSION}
MODEL = "google/gemma-3-4b-it"


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

    # Check session
    print("\n--- Session Check ---")
    try:
        sess = httpx.get(f"http://localhost:9800/sessions/{SESSION}", timeout=5)
        print(f"Session status: {sess.status_code}")
        if sess.status_code == 200:
            sd = sess.json()
            print(f"  Fact count: {sd.get('fact_count', '?')}")
            print(f"  Turns: {sd.get('turn_count', '?')}")
            print(f"  Goal: {sd.get('goal', '?')}")
    except Exception as e:
        print(f"Session check failed: {e}")

    # Check metrics
    print("\n--- Metrics ---")
    try:
        met = httpx.get("http://localhost:9800/metrics", timeout=5)
        md = met.json()
        for k, v in md.items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"Metrics check failed: {e}")


if __name__ == "__main__":
    main()
