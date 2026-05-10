"""/v1/chat/completions endpoint — full OpenAI-compatible proxy passthrough."""

from __future__ import annotations

import json

import structlog
from fastapi import APIRouter, Request
from starlette.responses import Response, StreamingResponse

from src.config import get_settings
from src.openai.errors import make_error_response
from src.openai.schemas import ChatCompletionRequest
from src.proxy.streaming import stream_with_capture

logger = structlog.get_logger()

router = APIRouter()


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Accept OpenAI chat completion requests and forward to upstream.

    Phase 1: full passthrough with SSE streaming, response capture,
    and OpenAI-formatted error handling.
    """
    settings = get_settings()

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        return make_error_response(400, "Invalid JSON in request body", "invalid_request_error")

    # Validate against schema
    try:
        req = ChatCompletionRequest(**body)
    except Exception as e:
        return make_error_response(400, f"Invalid request: {e}", "invalid_request_error")

    # Validate messages present
    if not req.messages:
        return make_error_response(
            400, "Messages array must not be empty", "invalid_request_error", param="messages"
        )

    # Build upstream request
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }
    request_body = json.dumps(body).encode("utf-8")

    if req.stream:
        return await _handle_streaming(request, upstream_url, headers, request_body)
    else:
        return await _handle_non_streaming(request, upstream_url, headers, request_body)


async def _handle_streaming(
    request: Request, url: str, headers: dict, body: bytes
) -> StreamingResponse:
    """Stream SSE chunks from upstream to client with response capture."""

    async def stream_generator():
        try:
            async with request.app.state.http_client.stream(
                "POST", url, headers=headers, content=body
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield f"data: {error_body.decode()}\n\n"
                    return

                capture = None
                async for line, cap in stream_with_capture(resp):
                    if cap is not None:
                        capture = cap
                        continue
                    if line:
                        yield line + "\n\n"

                # Phase 2 will fire extraction task here using capture
                if capture:
                    logger.debug(
                        "response_captured",
                        model=capture.model,
                        finish_reason=capture.finish_reason,
                        truncated=capture.truncated,
                        text_len=len(capture.get_full_text()),
                    )

        except httpx.TimeoutException:
            error = json.dumps(
                {"error": {"message": "Upstream request timed out", "type": "upstream_timeout", "code": "timeout"}}
            )
            yield f"data: {error}\n\n"
        except httpx.ConnectError as e:
            error = json.dumps(
                {"error": {"message": f"Upstream connection failed: {e}", "type": "upstream_error"}}
            )
            yield f"data: {error}\n\n"
        except Exception as e:
            logger.error("streaming_error", error=str(e), exc_info=True)
            error = json.dumps(
                {"error": {"message": f"Internal proxy error: {e}", "type": "server_error"}}
            )
            yield f"data: {error}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _handle_non_streaming(
    request: Request, url: str, headers: dict, body: bytes
) -> Response:
    """Handle non-streaming request — relay upstream response directly."""
    try:
        resp = await request.app.state.http_client.post(url, headers=headers, content=body)
    except httpx.TimeoutException:
        return make_error_response(
            504, "Upstream request timed out", "upstream_timeout", code="timeout"
        )
    except httpx.ConnectError as e:
        return make_error_response(502, f"Upstream connection failed: {e}", "upstream_error")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type="application/json",
    )
