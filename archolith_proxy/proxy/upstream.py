"""Upstream API request handling with retry — extracted from openai/chat.py.

Handles:
- Retryable status codes and exponential backoff
- Connection-level retries for both sync and streaming requests
- Upstream error metrics recording
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

logger = structlog.get_logger()

__all__ = [
    "RETRYABLE_STATUS_CODES",
    "upstream_request_with_retry",
]

# HTTP status codes that trigger a retry (transient errors, rate limits)
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


async def upstream_request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    content: bytes,
    max_retries: int = 3,
    backoff_base: float = 0.5,
) -> httpx.Response:
    """Send request to upstream with exponential backoff on transient errors.

    Returns the response on success or after exhausting retries.
    Raises httpx.ConnectError if all connection attempts fail.
    """
    from archolith_proxy.metrics import record_metric

    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = await client.post(url, headers=headers, content=content)
            if resp.status_code not in RETRYABLE_STATUS_CODES:
                return resp
            # Retryable status code
            if attempt < max_retries - 1:
                delay = backoff_base * (2**attempt)
                logger.warning(
                    "upstream_retryable_error",
                    status=resp.status_code,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
            else:
                return resp  # Last attempt, return whatever we got
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = backoff_base * (2**attempt)
                logger.warning(
                    "upstream_connection_retry",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay_s=delay,
                    error=str(e),
                )
                await asyncio.sleep(delay)
            else:
                record_metric("upstream_errors")
                raise
    # Should not reach here, but just in case
    record_metric("upstream_errors")
    raise last_exc or httpx.ConnectError("All retry attempts exhausted")

