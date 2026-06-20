"""Passthrough handling for chat completions."""

from __future__ import annotations

import json
import time

import httpx
import structlog
from fastapi import BackgroundTasks, Request
from fastapi.responses import Response, StreamingResponse

from archolith_proxy.openai.errors import make_error_response
from archolith_proxy.openai.helpers import _extract_response_text
from archolith_proxy.openai.schemas import ChatCompletionRequest
from archolith_proxy.proxy.upstream import upstream_request_with_retry
from archolith_proxy.trace.builder import TraceBuilder
from archolith_proxy.trace.store import get_trace_store

logger = structlog.get_logger()


def _estimate_input_tokens(messages: list[dict]) -> int:
    """Rough token estimate for input messages."""
    return sum(len(json.dumps(m)) // 4 for m in messages)


async def _handle_passthrough_stream(
    request: Request, body: dict, req: ChatCompletionRequest,
    trace_builder: TraceBuilder, background_tasks: BackgroundTasks,
    settings, request_start: float,
) -> Response:
    """Handle streaming passthrough (no context management)."""
    from archolith_proxy.proxy.streaming import ResponseCapture

    clean_model = req.model[: -len("-passthrough")]
    body["model"] = clean_model
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    upstream_headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }
    if req.stream:
        body.setdefault("stream_options", {})["include_usage"] = True
    request_body = json.dumps(body).encode("utf-8")
    t0 = time.monotonic()
    http_client = request.app.state.http_client
    pt_capture = ResponseCapture()

    async def _passthrough_stream():
        try:
            async with http_client.stream(
                "POST", upstream_url, headers=upstream_headers,
                content=request_body,
                timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    for line in chunk.decode("utf-8", errors="replace").split("\n"):
                        line = line.strip()
                        if line.startswith("data: ") and line != "data: [DONE]":
                            pt_capture.add_chunk(line[6:])
                    yield chunk
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("passthrough_stream_error", error=str(exc))

    latency_ms = (time.monotonic() - t0) * 1000

    async def _finalize_passthrough_trace():
        trace_builder.set_response(
            status=200, latency_ms=latency_ms,
            output_tokens=pt_capture.output_tokens,
            response_summary="(passthrough streaming)",
            cache_hit_tokens=pt_capture.cache_hit_tokens,
            cache_miss_tokens=pt_capture.cache_miss_tokens,
        )
        trace_builder.finalize_timing(time.monotonic())
        try:
            await get_trace_store().record(trace_builder.build())
        except Exception:
            pass

    background_tasks.add_task(_finalize_passthrough_trace)
    return StreamingResponse(
        _passthrough_stream(), status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        background=background_tasks,
    )


async def _handle_passthrough_non_stream(
    request: Request, body: dict, req: ChatCompletionRequest,
    trace_builder: TraceBuilder, background_tasks: BackgroundTasks,
    settings, request_start: float,
) -> Response:
    """Handle non-streaming passthrough."""
    clean_model = req.model[: -len("-passthrough")]
    body["model"] = clean_model
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    upstream_headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }
    request_body = json.dumps(body).encode("utf-8")
    t0 = time.monotonic()
    try:
        resp = await upstream_request_with_retry(
            client=request.app.state.http_client, url=upstream_url,
            headers=upstream_headers, content=request_body,
            max_retries=settings.upstream_max_retries,
            backoff_base=settings.upstream_retry_backoff_base_s,
        )
    except httpx.TimeoutException:
        return make_error_response(504, "Upstream request timed out", "upstream_timeout", code="timeout")
    except httpx.ConnectError as e:
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    latency_ms = (time.monotonic() - t0) * 1000
    response_data = resp.json() if resp.status_code == 200 else {}
    usage = response_data.get("usage", {})
    output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
    trace_builder.set_response(
        status=resp.status_code, latency_ms=latency_ms,
        output_tokens=output_tokens,
        response_summary=_extract_response_text(response_data)[:500],
        cache_hit_tokens=usage.get("prompt_cache_hit_tokens", 0) or 0,
        cache_miss_tokens=usage.get("prompt_cache_miss_tokens", 0) or 0,
    )
    trace_builder.finalize_timing(time.monotonic())

    async def _finalize_trace():
        try:
            await get_trace_store().record(trace_builder.build())
        except Exception:
            pass

    background_tasks.add_task(_finalize_trace)
    return Response(
        content=resp.content, status_code=resp.status_code,
        media_type="application/json", background=background_tasks,
    )
