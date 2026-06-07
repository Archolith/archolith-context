"""Pydantic models matching OpenAI API request/response shapes."""

# Required for forward references in ChatMessage.tool_calls (ToolCall) and ToolCall.function (ToolCallFunction)
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "ChatMessage",
    "ToolCallFunction",
    "ToolCall",
    "ToolFunction",
    "ToolDefinition",
    "ChatCompletionRequest",
    "ChatMessageResponse",
    "Choice",
    "Usage",
    "ChatCompletionResponse",
    "DeltaMessage",
    "ChunkChoice",
    "ChatCompletionChunk",
    "ModelObject",
    "ModelListResponse",
]

# --- Request Models ---


class ChatMessage(BaseModel):
    """A single message in a chat completion request."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    refusal: str | None = None


class ToolCallFunction(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ToolFunction(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolDefinition(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ChatCompletionRequest(BaseModel):
    """OpenAI chat completion request body."""

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    logprobs: bool | None = None
    top_logprobs: int | None = None
    n: int = 1
    user: str | None = None
    service_chat_id: str | None = None
    store: bool | None = None
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "allow"}


# --- Response Models ---


class ChatMessageResponse(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    refusal: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: ChatMessageResponse
    finish_reason: str | None = None
    logprobs: dict[str, Any] | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """OpenAI chat completion response body."""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:29]}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: list[Choice]
    usage: Usage | None = None
    system_fingerprint: str | None = None

    model_config = {"extra": "allow"}


# --- Streaming Models ---


class DeltaMessage(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    refusal: str | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: str | None = None
    logprobs: dict[str, Any] | None = None


class ChatCompletionChunk(BaseModel):
    """OpenAI SSE streaming chunk."""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:29]}")
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: list[ChunkChoice]
    usage: Usage | None = None
    system_fingerprint: str | None = None

    model_config = {"extra": "allow"}


# --- Models Endpoint ---


class ModelObject(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "unknown"


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelObject] = []
