"""Tests for upstream retry behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from archolith_proxy.metrics import get_metrics
from archolith_proxy.proxy.upstream import upstream_request_with_retry


URL = "https://upstream.test/v1/chat/completions"


@pytest.fixture(autouse=True)
def _reset_upstream_error_metric():
    metrics = get_metrics()
    old_value = metrics["upstream_errors"]
    metrics["upstream_errors"] = 0
    yield
    metrics["upstream_errors"] = old_value


async def _request_with_transport(
    transport: httpx.MockTransport,
    *,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport) as client:
        return await upstream_request_with_retry(
            client,
            URL,
            headers={"authorization": "Bearer test"},
            content=b"{}",
            max_retries=max_retries,
            backoff_base=backoff_base,
        )


def _status_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, json={"status": status_code})


@pytest.mark.asyncio
async def test_first_attempt_success_returns_immediately() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _status_response(200)

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        resp = await _request_with_transport(httpx.MockTransport(handler))

    assert resp.status_code == 200
    assert attempts == 1
    sleep.assert_not_awaited()
    assert get_metrics()["upstream_errors"] == 0


@pytest.mark.asyncio
async def test_429_retried_with_backoff_then_succeeds() -> None:
    statuses = [429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return _status_response(statuses.pop(0))

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        resp = await _request_with_transport(httpx.MockTransport(handler))

    assert resp.status_code == 200
    assert statuses == []
    sleep.assert_awaited_once_with(0.5)
    assert get_metrics()["upstream_errors"] == 0


@pytest.mark.asyncio
async def test_503_retried_three_times_then_succeeds_on_last_attempt() -> None:
    statuses = [503, 503, 503, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return _status_response(statuses.pop(0))

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        resp = await _request_with_transport(httpx.MockTransport(handler), max_retries=4)

    assert resp.status_code == 200
    assert statuses == []
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0, 2.0]
    assert get_metrics()["upstream_errors"] == 0


@pytest.mark.asyncio
async def test_all_retryable_status_attempts_return_final_response() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _status_response(503)

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        resp = await _request_with_transport(httpx.MockTransport(handler))

    assert resp.status_code == 503
    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
    assert get_metrics()["upstream_errors"] == 0


@pytest.mark.asyncio
async def test_connect_error_retries_and_raises_after_max() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connect failed", request=request)

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        with pytest.raises(httpx.ConnectError):
            await _request_with_transport(httpx.MockTransport(handler))

    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
    assert get_metrics()["upstream_errors"] == 1


@pytest.mark.asyncio
async def test_timeout_exception_retries_and_raises_after_max() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.TimeoutException("timed out", request=request)

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        with pytest.raises(httpx.TimeoutException):
            await _request_with_transport(httpx.MockTransport(handler))

    assert attempts == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]
    assert get_metrics()["upstream_errors"] == 1


@pytest.mark.asyncio
async def test_400_does_not_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _status_response(400)

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        resp = await _request_with_transport(httpx.MockTransport(handler))

    assert resp.status_code == 400
    assert attempts == 1
    sleep.assert_not_awaited()
    assert get_metrics()["upstream_errors"] == 0


@pytest.mark.asyncio
async def test_exponential_backoff_delay_sequence() -> None:
    statuses = [503, 503, 503, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        return _status_response(statuses.pop(0))

    sleep = AsyncMock()
    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", sleep):
        await _request_with_transport(httpx.MockTransport(handler), max_retries=4, backoff_base=0.25)

    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.5, 1.0]


@pytest.mark.asyncio
async def test_metric_recorded_once_on_final_connection_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed", request=request)

    with patch("archolith_proxy.proxy.upstream.asyncio.sleep", AsyncMock()):
        with pytest.raises(httpx.ConnectError):
            await _request_with_transport(httpx.MockTransport(handler), max_retries=2)

    assert get_metrics()["upstream_errors"] == 1
