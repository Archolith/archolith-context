"""/v1/chat/completions endpoint — Phase 0 passthrough shell."""

import json
import time

import structlog
from fastapi import APIRouter, Request
from starlette.responses import Response, StreamingResponse

from src.config import get_settings
from src.openai.errors import (
    InvalidRequestError,
    UpstreamError,
    UpstreamTimeoutError,
    make_error_response,
)
from src.openai.schemas import ChatCompletionRequest

logger = structlog.get_logger()

router = APIRouter()

SSE_PREFIX = "data: "
SSE_DONE = "data: [DONE]\n\n"


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Accept OpenAI chat completion requests and forward to upstream.

    Phase 0: passthrough with no context modification.
    Phase 1+: will add session resolution, assembly, extraction.
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
        return make_error_response(
            400,
            f"Invalid request: {e}",
            "invalid_request_error",
        )

    # Validate messages present
    if not req.messages:
        return make_error_response(
            400,
            "Messages array must not be empty",
            "invalid_request_error",
            param="messages",
        )

    # Forward to upstream
    upstream_url = f"{settings.upstream_api_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
    }

    if req.stream:
        return await _handle_streaming(request, upstream_url, headers, body)
    else:
        return await _handle_non_streaming(request, upstream_url, headers, body)


async def _handle_streaming(request: Request, url: str, headers: dict, body: dict) -> StreamingResponse:
    """Stream SSE chunks from upstream to client."""

    async def stream_generator():
        try:
            async with request.app.state.http_client.stream(
                "POST", url, headers=headers, content=json.dumps(body).encode()
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    yield error_body.decode()
                    return

                async for line in resp.aiter_lines():
                    if line:
                        yield line + "\n\n"
        except Exception as e:
            logger.error("streaming_error", error=str(e))
            yield json.dumps({"error": {"message": str(e), "type": "upstream_error"}}) + "\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _handle_non_streaming(request: Request, url: str, headers: dict, body: dict) -> Response:
    """Handle non-streaming request."""
    try:
        resp = await request.app.state.http_client.post(
            url,
            headers=headers,
            content=json.dumps(body).encode(),
        )
    except Exception as e:
        logger.error("upstream_error", error=str(e))
        return make_error_response(502, f"Upstream request failed: {e}", "upstream_error")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": "application/json"},
    )
