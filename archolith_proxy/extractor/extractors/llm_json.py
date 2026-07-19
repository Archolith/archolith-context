"""LLM-based JSON structured extraction helper."""

from __future__ import annotations

import json
from typing import Any, Dict


async def call_llm_for_structured_extraction(
    http_client: Any,
    system_prompt: str,
    user_prompt: str,
    model: str,
    base_url: str,
    api_key: str,
) -> Dict[str, Any]:
    """
    Call the LLM with a prompt and expect structured JSON output.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = await http_client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload).encode(),
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        return {"error": str(e)}