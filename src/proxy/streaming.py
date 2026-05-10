"""SSE streaming passthrough + response capture.

Streams SSE chunks from upstream to client in real-time while
simultaneously capturing the full response into a buffer for
post-hoc extraction. Buffer is capped at MAX_BUFFER_SIZE bytes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import httpx
import structlog

logger = structlog.get_logger()

# Maximum bytes to capture for extraction (512KB).
# Truncation preserves metadata (tool names, file paths) and drops
# verbose content (full file reads, long logs).
MAX_BUFFER_SIZE = 512 * 1024


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
        """Reassemble the full assistant response text from captured chunks."""
        texts: list[str] = []
        for chunk_data in self._chunks:
            try:
                data = json.loads(chunk_data)
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        texts.append(content)
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
        return "".join(texts)

    @property
    def model(self) -> str:
        return self._model

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason

    @property
    def truncated(self) -> bool:
        return self._truncated


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
