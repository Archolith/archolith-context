"""Tests for D3 — streaming recall decision timeout is tunable and late sentinels observable.

A recall sentinel arriving after the decision window is not intercepted; it must
be logged (streaming_recall_sentinel_after_timeout) rather than silently bypassed.
The decision timeout is configurable via the function parameter.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from archolith_proxy.proxy.streaming import stream_with_recall_detection


async def _resp(lines: list[str]):
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200

    async def aiter_lines():
        for line in lines:
            yield line

    response.aiter_lines = aiter_lines
    return response


async def _drain(resp, **kwargs):
    async for _line, _result, _cap in stream_with_recall_detection(
        resp, "__archolith_recall", **kwargs
    ):
        pass


class TestLateSentinelWarning:
    @pytest.mark.asyncio
    async def test_late_recall_sentinel_after_content_warns(self):
        # Content first -> committed to passthrough; recall appears later.
        lines = [
            'data: {"choices":[{"delta":{"content":"thinking"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"__archolith_recall"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        resp = await _resp(lines)
        with patch("archolith_proxy.proxy.streaming.logger") as mock_log:
            await _drain(resp)
        events = [c.args[0] for c in mock_log.warning.call_args_list if c.args]
        assert "streaming_recall_sentinel_after_timeout" in events

    @pytest.mark.asyncio
    async def test_no_warning_when_no_late_sentinel(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        resp = await _resp(lines)
        with patch("archolith_proxy.proxy.streaming.logger") as mock_log:
            await _drain(resp)
        events = [c.args[0] for c in mock_log.warning.call_args_list if c.args]
        assert "streaming_recall_sentinel_after_timeout" not in events


class TestConfigurableTimeout:
    @pytest.mark.asyncio
    async def test_negative_timeout_forces_decision_timeout(self):
        # A non-deciding first line + immediate (negative) timeout -> timeout path.
        lines = [
            'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"late"},"finish_reason":null}]}',
            'data: [DONE]',
        ]
        resp = await _resp(lines)
        with patch("archolith_proxy.proxy.streaming.logger") as mock_log:
            await _drain(resp, decision_timeout_s=-1.0)
        events = [c.args[0] for c in mock_log.warning.call_args_list if c.args]
        assert "streaming_recall_decision_timeout" in events
