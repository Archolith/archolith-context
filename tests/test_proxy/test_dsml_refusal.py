"""Tests for non-streaming DSML/tool-call leak refusal (retry once, then strip).

The proxy injects a no-tool hint to discourage DeepSeek from emitting DSML tool-call
markup, but the model can still leak it. _refuse_dsml_leak catches a leaked response
on a no-tools DeepSeek request and retries once with a stronger instruction, then
strips as a safety net.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from archolith_proxy.config import get_settings, reset_settings
from archolith_proxy.openai.non_streaming import _refuse_dsml_leak

DSML = "<｜｜DSML｜｜tool_calls> read_file foo.py"
CLEAN = "Here is the answer:\n```python\nx = 1\n```"


def _resp(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def _request_returning(retry_content: str) -> SimpleNamespace:
    """A fake request whose upstream client returns retry_content for any POST."""
    def handler(req: httpx.Request) -> httpx.Response:
        return _resp(retry_content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=client)))


def _body(model: str = "deepseek-chat", tools=None) -> bytes:
    b = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if tools is not None:
        b["tools"] = tools
    return json.dumps(b).encode()


@pytest.fixture(autouse=True)
def _settings():
    reset_settings()
    yield
    reset_settings()


@pytest.mark.asyncio
async def test_retry_recovers_clean_response():
    """Leaked initial response -> retry returns clean -> use the clean retry."""
    request = _request_returning(CLEAN)
    out = await _refuse_dsml_leak(
        resp=_resp(DSML), request=request, url="http://up/v1/chat/completions",
        headers={}, body=_body(), settings=get_settings(), session_id="s1",
    )
    content = out.json()["choices"][0]["message"]["content"]
    assert content == CLEAN
    assert "DSML" not in content


@pytest.mark.asyncio
async def test_retry_still_leaks_then_stripped():
    """Leaked initial AND leaked retry -> markup stripped as a safety net."""
    request = _request_returning(DSML)  # retry also leaks
    out = await _refuse_dsml_leak(
        resp=_resp(DSML), request=request, url="http://up/v1/chat/completions",
        headers={}, body=_body(), settings=get_settings(), session_id="s1",
    )
    content = out.json()["choices"][0]["message"]["content"]
    assert "DSML" not in content  # stripped (may be empty)


@pytest.mark.asyncio
async def test_clean_response_unchanged():
    """A clean response is returned untouched (no retry)."""
    original = _resp(CLEAN)
    out = await _refuse_dsml_leak(
        resp=original, request=_request_returning(CLEAN),
        url="http://up/v1/chat/completions", headers={}, body=_body(),
        settings=get_settings(), session_id="s1",
    )
    assert out is original  # not rebuilt


@pytest.mark.asyncio
async def test_non_deepseek_not_gated():
    """Leak on a non-DeepSeek model passes through (gating)."""
    original = _resp(DSML)
    out = await _refuse_dsml_leak(
        resp=original, request=_request_returning(CLEAN),
        url="http://up/v1/chat/completions", headers={}, body=_body(model="gpt-4o-mini"),
        settings=get_settings(), session_id="s1",
    )
    assert out is original  # untouched


@pytest.mark.asyncio
async def test_real_tools_not_gated():
    """Leak on a request WITH tools passes through (tool-calls are intentional)."""
    original = _resp(DSML)
    out = await _refuse_dsml_leak(
        resp=original, request=_request_returning(CLEAN),
        url="http://up/v1/chat/completions", headers={},
        body=_body(tools=[{"type": "function", "function": {"name": "x"}}]),
        settings=get_settings(), session_id="s1",
    )
    assert out is original  # untouched
