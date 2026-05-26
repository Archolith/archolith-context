"""SSE streaming passthrough + response capture + recall tool interception.

Streams SSE chunks from upstream to client in real-time while
simultaneously capturing the full response into a buffer for
post-hoc extraction. Buffer is capped at MAX_BUFFER_SIZE bytes.

When recall tool injection is active, uses a buffer-and-decide approach:
1. Buffer chunks until we know the model's intent (content vs tool call)
2. If model calls __archolith_recall, buffer the full stream,
   execute recall, re-send non-streaming, then stream the second response
3. Otherwise, flush buffer and switch to passthrough
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator

import httpx
import structlog
from starlette.responses import Response, StreamingResponse

logger = structlog.get_logger()

# Maximum bytes to capture for extraction (512KB).
# Truncation preserves metadata (tool names, file paths) and drops
# verbose content (full file reads, long logs).
MAX_BUFFER_SIZE = 512 * 1024

# Maximum time (seconds) to buffer before deciding the model's intent.
# If we haven't detected content or a tool name within this window,
# flush the buffer and switch to passthrough.
DECISION_TIMEOUT_S = 5.0


def _flatten_content(content: object) -> str:
    """Flatten OpenAI-style content blocks into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            parts.append(_flatten_content(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if nested is not None:
            return _flatten_content(nested)
    return ""


class ResponseCapture:
    """Captures streaming response content into a buffer for extraction.

    Thread-safe for single-producer (streaming chunks) single-consumer
    (post-hoc extraction task).
    """

    def __init__(self, max_size: int = MAX_BUFFER_SIZE) -> None:
        self._chunks: list[str] = []
        self._size = 0
        self._max_size = max_size
        self._truncated = False
        self._model = ""
        self._finish_reason: str | None = None
        self._direct_text: str | None = None  # For non-streaming responses

    def add_chunk(self, chunk_data: str) -> None:
        """Add a parsed SSE chunk to the buffer."""
        if self._truncated:
            return

        chunk_size = len(chunk_data.encode("utf-8"))
        if self._size + chunk_size > self._max_size:
            self._truncated = True
            logger.warning(
                "response_buffer_truncated",
                size=self._size,
                max_size=self._max_size,
            )
            return

        self._chunks.append(chunk_data)
        self._size += chunk_size

        # Extract metadata from chunk
        try:
            data = json.loads(chunk_data)
            if data.get("model"):
                self._model = data["model"]
            choices = data.get("choices", [])
            if choices:
                fr = choices[0].get("finish_reason")
                if fr:
                    self._finish_reason = fr
        except (json.JSONDecodeError, IndexError, KeyError):
            pass

    def get_full_text(self) -> str:
        """Reassemble the full assistant response text from captured chunks.

        Handles both streaming chunks (delta.content) and non-streaming
        responses (message.content) stored via add_chunk or set_non_streaming_response.
        """
        # If a non-streaming response was stored directly, return its text
        if self._direct_text is not None:
            return self._direct_text

        texts: list[str] = []
        for chunk_data in self._chunks:
            try:
                data = json.loads(chunk_data)
                choices = data.get("choices", [])
                if choices:
                    # Streaming format: delta.content
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        texts.append(content)
                    # Non-streaming format: message.content
                    if not delta:
                        message = choices[0].get("message", {})
                        content = _flatten_content(message.get("content"))
                        if content:
                            texts.append(content)
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
        return "".join(texts)

    def set_non_streaming_response(self, response_data: dict) -> None:
        """Store a non-streaming response dict for extraction.

        This is used when the proxy intercepts a recall tool call during
        streaming and re-sends as non-streaming. The response is in
        non-streaming format (message.content, not delta.content), so
        get_full_text() needs to handle it differently.
        """
        choices = response_data.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            self._direct_text = _flatten_content(message.get("content"))
            fr = choices[0].get("finish_reason")
            if fr:
                self._finish_reason = fr
        if response_data.get("model"):
            self._model = response_data["model"]

    @property
    def model(self) -> str:
        return self._model

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason

    @property
    def truncated(self) -> bool:
        return self._truncated


class StreamingToolCallAccumulator:
    """Accumulates streaming SSE tool_call deltas into complete tool_call objects.

    OpenAI streaming format for tool calls:
    - First chunk: delta.tool_calls[i] = {index, id, type, function: {name, arguments: ""}}
    - Subsequent chunks: delta.tool_calls[i] = {index, function: {arguments: "partial"}}

    This accumulator reassembles the fragments into complete tool_call dicts
    matching the non-streaming format.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict] = {}
        self._complete = False

    def add_delta(self, delta_tool_calls: list[dict]) -> None:
        """Process a delta.tool_calls array from a streaming chunk."""
        for tc_delta in delta_tool_calls:
            if not isinstance(tc_delta, dict):
                continue
            idx = tc_delta.get("index", 0)
            if idx not in self._calls:
                self._calls[idx] = {
                    "id": tc_delta.get("id", ""),
                    "type": tc_delta.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                }
            entry = self._calls[idx]

            # Update ID if provided
            if tc_delta.get("id"):
                entry["id"] = tc_delta["id"]

            # Update function name/arguments if provided
            func_delta = tc_delta.get("function", {})
            if func_delta.get("name"):
                entry["function"]["name"] = func_delta["name"]
            if func_delta.get("arguments"):
                entry["function"]["arguments"] += func_delta["arguments"]

    def mark_complete(self) -> None:
        """Mark the tool call stream as complete (finish_reason=tool_calls received)."""
        self._complete = True

    @property
    def is_complete(self) -> bool:
        return self._complete

    @property
    def tool_calls(self) -> list[dict]:
        """Return the accumulated tool calls in index order."""
        return [self._calls[i] for i in sorted(self._calls.keys()) if i in self._calls]

    @property
    def first_tool_name(self) -> str | None:
        """Return the function name of the first tool call, or None if not yet known."""
        if 0 in self._calls:
            return self._calls[0]["function"].get("name") or None
        return None


def _parse_sse_line(line: str) -> dict | None:
    """Parse a SSE data line into a JSON dict. Returns None for non-data lines or [DONE]."""
    if not line.startswith("data: "):
        return None
    chunk_data = line[len("data: "):]
    if chunk_data.strip() == "[DONE]":
        return None
    try:
        return json.loads(chunk_data)
    except (json.JSONDecodeError, KeyError):
        return None


def _assemble_streaming_response(
    chunks: list[str], tool_calls: StreamingToolCallAccumulator,
) -> dict:
    """Reassemble buffered streaming chunks into a non-streaming-style response dict.

    This is used to extract the model's message for recall re-send.
    """
    content_parts: list[str] = []
    model = ""
    finish_reason = "stop"
    response_id = ""
    created = 0

    for chunk_data in chunks:
        try:
            data = json.loads(chunk_data)
            if data.get("model"):
                model = data["model"]
            if data.get("id"):
                response_id = data["id"]
            if data.get("created"):
                created = data["created"]
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if delta.get("content"):
                    content_parts.append(delta["content"])
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
        except (json.JSONDecodeError, IndexError, KeyError):
            continue

    message: dict = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_calls.tool_calls:
        message["tool_calls"] = tool_calls.tool_calls
        finish_reason = "tool_calls"

    return {
        "id": response_id or "chatcmpl-recall",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _non_streaming_to_sse(response_data: dict) -> list[str]:
    """Convert a non-streaming response dict into SSE-format lines for streaming to client.

    Produces two SSE events: the content chunk and the [DONE] terminator.
    """
    lines: list[str] = []

    # First chunk: role + content
    message = response_data.get("choices", [{}])[0].get("message", {})
    first_chunk = {
        "id": response_data.get("id", "chatcmpl-recall"),
        "object": "chat.completion.chunk",
        "created": response_data.get("created", int(time.time())),
        "model": response_data.get("model", ""),
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant"},
            "finish_reason": None,
        }],
    }
    lines.append(f"data: {json.dumps(first_chunk)}")

    # Content chunk(s) — split into reasonable chunks for streaming feel
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls")

    if content:
        content_chunk = {
            "id": response_data.get("id", "chatcmpl-recall"),
            "object": "chat.completion.chunk",
            "created": response_data.get("created", int(time.time())),
            "model": response_data.get("model", ""),
            "choices": [{
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }],
        }
        lines.append(f"data: {json.dumps(content_chunk)}")

    if tool_calls:
        # Emit tool_call deltas: first chunk with id+name, then argument chunks
        for i, tc in enumerate(tool_calls):
            # First delta: id, type, function.name
            name_delta = {
                "id": response_data.get("id", "chatcmpl-recall"),
                "object": "chat.completion.chunk",
                "created": response_data.get("created", int(time.time())),
                "model": response_data.get("model", ""),
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": i,
                            "id": tc.get("id", f"call_{i}"),
                            "type": tc.get("type", "function"),
                            "function": {"name": tc["function"]["name"], "arguments": ""},
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            lines.append(f"data: {json.dumps(name_delta)}")

            # Argument delta
            args = tc.get("function", {}).get("arguments", "")
            if args:
                args_delta = {
                    "id": response_data.get("id", "chatcmpl-recall"),
                    "object": "chat.completion.chunk",
                    "created": response_data.get("created", int(time.time())),
                    "model": response_data.get("model", ""),
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": i,
                                "function": {"arguments": args},
                            }],
                        },
                        "finish_reason": None,
                    }],
                }
                lines.append(f"data: {json.dumps(args_delta)}")

    # Final chunk with finish_reason
    finish_reason = response_data.get("choices", [{}])[0].get("finish_reason", "stop")
    final_chunk = {
        "id": response_data.get("id", "chatcmpl-recall"),
        "object": "chat.completion.chunk",
        "created": response_data.get("created", int(time.time())),
        "model": response_data.get("model", ""),
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": finish_reason,
        }],
    }
    lines.append(f"data: {json.dumps(final_chunk)}")
    lines.append("data: [DONE]")

    return lines


async def yield_as_sse(response_data: dict) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted chunks from a non-streaming response dict.

    Each yielded string is a complete ``data: {...}\\n\\n`` event ready to
    write to the client.  Use this inside async generators that are already
    yielding SSE lines — it keeps all ``\\n\\n`` formatting in one place.
    """
    for line in _non_streaming_to_sse(response_data):
        yield line + "\n\n"


def _wrap_response_as_sse(resp: Response) -> StreamingResponse:
    """Convert a non-streaming JSON chat completion response to SSE streaming format.

    Used when an interception path required non-streaming upstream but the
    original client requested streaming.  Produces a well-formed SSE stream
    with role, content, tool_calls, and finish_reason deltas so the streaming
    client parses it normally.

    Error propagation: when upstream returns status >= 400 the error body is
    forwarded as a data event followed by [DONE] so the client can see and
    handle the error instead of receiving a silent empty stream.
    """
    if resp.status_code >= 400:
        error_body = (
            resp.body.decode("utf-8") if isinstance(resp.body, bytes)
            else (resp.body or b"{}").decode("utf-8")
        )

        async def _sse_error() -> AsyncGenerator[str, None]:
            yield f"data: {error_body}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _sse_error(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    try:
        data = json.loads(resp.body.decode("utf-8") if isinstance(resp.body, bytes) else resp.body)
    except Exception:
        data = {}

    return StreamingResponse(
        yield_as_sse(data),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        background=resp.background,
    )


async def stream_with_capture(
    upstream_response: httpx.Response,
) -> AsyncGenerator[tuple[str, ResponseCapture | None], None]:
    """Yield raw SSE lines from upstream while capturing parsed chunks.

    Yields:
    (raw_line, None) for each SSE line — caller should relay to client.
    After stream ends, yields one final ("", capture) with the full capture.
    """
    capture = ResponseCapture()

    async for line in upstream_response.aiter_lines():
        if not line:
            continue

        yield (line, None)

        # Parse and capture data lines (skip [DONE] and empty)
        if line.startswith("data: ") and not line.endswith("[DONE]"):
            chunk_data = line[len("data: "):]
            if chunk_data.strip():
                capture.add_chunk(chunk_data)

    # Signal capture is complete
    yield ("", capture)


class StreamingRecallResult:
    """Result of streaming recall detection.

    Attributes:
        is_recall: True if the model called __archolith_recall.
        buffered_lines: All SSE lines buffered during the detection phase.
        accumulator: The tool call accumulator (for extracting the full tool call).
        capture: The response capture for extraction.
    """

    def __init__(
        self,
        is_recall: bool = False,
        buffered_lines: list[str] | None = None,
        accumulator: StreamingToolCallAccumulator | None = None,
        capture: ResponseCapture | None = None,
        remaining_lines: list[str] | None = None,
    ) -> None:
        self.is_recall = is_recall
        self.buffered_lines = buffered_lines or []
        self.accumulator = accumulator
        self.capture = capture or ResponseCapture()
        self.remaining_lines = remaining_lines or []


async def stream_with_recall_detection(
    upstream_response: httpx.Response,
    recall_tool_name: str,
    decision_timeout_s: float = DECISION_TIMEOUT_S,
) -> AsyncGenerator[tuple[str, StreamingRecallResult | None, ResponseCapture | None], None]:
    """Stream SSE with recall tool call detection.

    Phase 1 (Decision): Buffer chunks until the model's intent is known:
    - If delta.content appears → not a recall, switch to passthrough
    - If delta.tool_calls[i].function.name == recall_tool_name → recall detected
    - If delta.tool_calls[i].function.name != recall_tool_name → not recall, passthrough
    - Timeout → flush buffer, passthrough

    Phase 2a (Passthrough): Yield buffered lines, then continue streaming directly.

    Phase 2b (Recall): Buffer the entire stream, then yield a StreamingRecallResult
    with the complete accumulator for the caller to handle the recall.

    Yields:
        (line, None, None) — SSE line to relay to client (passthrough mode)
        ("", result, None) — Recall detection result (when recall is detected)
        ("", None, capture) — Final capture (when stream ends in passthrough mode)
    """
    capture = ResponseCapture()
    accumulator = StreamingToolCallAccumulator()
    buffered_lines: list[str] = []
    decision_made = False
    is_recall = False
    recall_buffered_lines: list[str] = []
    start_time = time.monotonic()

    async for line in upstream_response.aiter_lines():
        if not line:
            continue

        # Capture for extraction regardless of mode
        if line.startswith("data: ") and not line.endswith("[DONE]"):
            chunk_data = line[len("data: "):]
            if chunk_data.strip():
                capture.add_chunk(chunk_data)

        if not decision_made:
            # Still in decision phase — buffer the line
            buffered_lines.append(line)

            # Parse the chunk to check for content or tool calls
            parsed = _parse_sse_line(line)
            if parsed:
                choices = parsed.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})

                    # Check for content → model is producing text, not a tool call
                    if delta.get("content") is not None:
                        decision_made = True
                        is_recall = False

                    # Check for tool calls
                    if delta.get("tool_calls"):
                        accumulator.add_delta(delta["tool_calls"])
                        first_name = accumulator.first_tool_name
                        if first_name == recall_tool_name:
                            decision_made = True
                            is_recall = True
                        elif first_name is not None and first_name != recall_tool_name:
                            # First tool call is NOT recall — passthrough
                            # (recall might appear as a secondary call, but we
                            # don't intercept mid-stream for that case)
                            decision_made = True
                            is_recall = False

                    # Check finish_reason for tool_calls completion
                    fr = choices[0].get("finish_reason")
                    if fr == "tool_calls":
                        accumulator.mark_complete()

            # Timeout check
            if not decision_made and (time.monotonic() - start_time) > decision_timeout_s:
                logger.warning("streaming_recall_decision_timeout")
                decision_made = True
                is_recall = False

            # Handle the decision on the same iteration it was made
            if decision_made:
                if is_recall:
                    # Copy buffered lines into recall buffer (current line
                    # is already in buffered_lines). Continue to next line
                    # to avoid duplicating it in recall_buffered_lines below.
                    recall_buffered_lines = list(buffered_lines)
                    continue
                else:
                    # Flush buffered lines and switch to passthrough
                    for bl in buffered_lines:
                        yield (bl, None, None)
                    buffered_lines = []
                    # The current line was already yielded above (it's in
                    # buffered_lines). Skip the passthrough yield for this
                    # iteration.
                    continue

        if decision_made and is_recall:
            # Buffer the rest of the stream for recall processing
            recall_buffered_lines.append(line)
            # Continue accumulating tool call deltas
            parsed = _parse_sse_line(line)
            if parsed:
                choices = parsed.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    if delta.get("tool_calls"):
                        accumulator.add_delta(delta["tool_calls"])
                    fr = choices[0].get("finish_reason")
                    if fr == "tool_calls":
                        accumulator.mark_complete()
            continue

        if decision_made and not is_recall:
            # Passthrough — yield lines directly
            yield (line, None, None)

    # Stream complete
    if is_recall:
        # Return the recall result
        result = StreamingRecallResult(
            is_recall=True,
            buffered_lines=recall_buffered_lines,
            accumulator=accumulator,
            capture=capture,
        )
        yield ("", result, None)
    else:
        # Signal capture is complete
        yield ("", None, capture)
