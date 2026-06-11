"""Curator LLM loop — tool-calling context manager adapted from delegate_server.py.

Ports _run_agent_native from cth.mcp.delegate,
adapted for context curation: 4 max iterations, async tool dispatch,
returns CuratorResult | None.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

from openai import AsyncOpenAI
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

import structlog

from archolith_proxy.curator.result import CuratorFailure, CuratorResult, CuratorToolCall
from archolith_proxy.curator.schemas import ALL_CURATOR_TOOLS
from archolith_proxy.curator.tools import TOOL_HANDLERS

logger = structlog.get_logger()

_RETRYABLE_ERRORS = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)

_ERROR_WINDOW_SIZE = 4


def _estimate_tokens(text: str) -> int:
    """Rough token estimate — ~3 chars per token (conservative for code)."""
    return max(1, len(text) // 3)


# Max content length per message in failure diagnostics
_DIAG_MAX_CONTENT = 2000


def _serialize_message(msg) -> dict:
    """Convert a message (dict or openai object) to a serializable dict.

    Truncates large content to keep failure records bounded.
    """
    if isinstance(msg, dict):
        d = dict(msg)
        c = d.get("content")
        if isinstance(c, str) and len(c) > _DIAG_MAX_CONTENT:
            d["content"] = c[:_DIAG_MAX_CONTENT] + f"... [{len(c)} chars]"
        return d
    if hasattr(msg, "model_dump"):
        return msg.model_dump(exclude_none=True)
    return {"content": str(msg)[:_DIAG_MAX_CONTENT]}


def _save_failure_diagnostic(
    session_id: str,
    failure_reason: str,
    messages: list,
    total_tool_calls: int,
    curated_paths: set[str],
    retained_turn_numbers: list[int] | None,
    iteration: int,
    error_detail: str = "",
) -> None:
    """Persist a curator failure record to disk for later analysis.

    Writes a JSONL line to <trace_dir>/curator_failures.jsonl containing
    the full curator conversation, failure reason, and accumulated state.
    Non-fatal — never raises.
    """
    try:
        from archolith_proxy.config import get_settings
        settings = get_settings()
        trace_dir = settings.trace_dir
        if not trace_dir:
            return

        out_dir = Path(trace_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        failure = CuratorFailure(
            session_id=session_id,
            failure_reason=failure_reason,
            messages=[_serialize_message(m) for m in messages],
            tool_calls_made=total_tool_calls,
            curated_paths=sorted(curated_paths),
            retained_turn_numbers=retained_turn_numbers,
            iterations_completed=iteration + 1,
            error_detail=str(error_detail)[:500],
        )

        path = out_dir / "curator_failures.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(failure.model_dump_json() + "\n")

        logger.info(
            "curator_failure_saved",
            session_id=session_id,
            reason=failure_reason,
            tool_calls=total_tool_calls,
            iterations=iteration + 1,
        )
    except Exception:
        logger.warning("curator_failure_save_error", session_id=session_id, exc_info=True)


async def _llm_call_with_retry(
    client: AsyncOpenAI,
    max_retries: int,
    base_delay: float,
    **kwargs,
):
    """Call client.chat.completions.create with exponential backoff.

    Retries on: 429, connection errors, timeouts, 5xx.
    Respects Retry-After headers on 429 responses.
    Non-retryable errors propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return await client.chat.completions.create(**kwargs)
        except _RETRYABLE_ERRORS as exc:
            if attempt >= max_retries:
                raise
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                raw = getattr(response, "headers", {}).get("retry-after")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        pass
            if retry_after is None:
                cap = min(base_delay * (2 ** attempt), 60.0)
                retry_after = random.uniform(0.0, cap)
            logger.debug(
                "curator_llm_retry",
                attempt=attempt + 1,
                max_retries=max_retries + 1,
                error_type=type(exc).__name__,
                retry_after_s=round(retry_after, 1),
            )
            await asyncio.sleep(retry_after)


async def _run_curator_native(
    client: AsyncOpenAI,
    session_id: str,
    user_prompt: str,
    max_iterations: int,
    system_prompt: str,
    model: str,
    tool_set: list[dict] | None = None,
) -> tuple[CuratorResult | None, list[CuratorToolCall], str]:
    """Curator loop using native OpenAI-compatible tool calling.

    Adapted from delegate_server._run_agent_native for context curation:
    - No working_dir, checkpoint, read_only, file_context
    - session_id passed to all tool dispatches
    - Tracks curated_paths and tool_calls_used
    - Accepts optional tool_set parameter for filtered tool sets
      (e.g., ASSEMBLER_TOOLS for the inline assembler).
      Defaults to ALL_CURATOR_TOOLS for backward compatibility.
    - Returns (CuratorResult, tool_log, "") on success,
      or (None, tool_log, failure_reason) on error/timeout/max iterations
    """
    tools = tool_set if tool_set is not None else ALL_CURATOR_TOOLS
    allowed_tools = {s["function"]["name"] for s in tools}

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    error_window: list[tuple[str, str]] = []
    total_tool_calls = 0
    iterations_used = 0
    curated_paths: set[str] = set()
    retained_turn_numbers: list[int] | None = None
    tool_log: list[CuratorToolCall] = []
    # Token usage accumulated across all LLM calls
    curator_prompt_tokens: int = 0
    curator_completion_tokens: int = 0
    # Track seen queries to detect wasteful re-fetches of search results
    _seen_queries: set[str] = set()

    for iteration in range(max_iterations):
        iterations_used = iteration + 1
        logger.debug(
            "curator_iteration",
            iteration=iteration + 1,
            max_iterations=max_iterations,
            session_id=session_id,
        )
        try:
            response = await _llm_call_with_retry(
                client,
                max_retries=2,
                base_delay=1.0,
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
            )
            if not response.choices:
                logger.warning("curator_empty_response", session_id=session_id)
                _save_failure_diagnostic(session_id, "empty_response", messages,
                    total_tool_calls, curated_paths, retained_turn_numbers, iteration)
                return None, tool_log, "empty_response"
            choice = response.choices[0]
        except Exception as exc:
            logger.warning("curator_llm_error", session_id=session_id, error=str(exc))
            _save_failure_diagnostic(session_id, "llm_error", messages,
                total_tool_calls, curated_paths, retained_turn_numbers, iteration,
                error_detail=str(exc))
            return None, tool_log, f"llm_error: {str(exc)[:200]}"

        # Capture token usage from the LLM response
        if response.usage:
            curator_prompt_tokens += response.usage.prompt_tokens or 0
            curator_completion_tokens += response.usage.completion_tokens or 0

        tool_count = len(choice.message.tool_calls or [])
        content_len = len((choice.message.content or "").strip())
        logger.info(
            "curator_response",
            iteration=iteration + 1,
            finish_reason=choice.finish_reason,
            tool_calls=tool_count,
            content_len=content_len,
            session_id=session_id,
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
            cumulative_prompt_tokens=curator_prompt_tokens,
            cumulative_completion_tokens=curator_completion_tokens,
        )

        if choice.finish_reason == "stop":
            content = (choice.message.content or "").strip()
            if not content:
                # Model returned stop with empty content — retry once with an
                # explicit nudge before giving up. This recovers from transient
                # LLM behaviour where it suppresses its inline answer when
                # tool_choice="auto" confuses it.
                if iteration == 0:
                    logger.info(
                        "curator_empty_final_retry",
                        session_id=session_id,
                        tool_calls=total_tool_calls,
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            "Please provide the context block now. "
                            "Use the format in the system prompt. "
                            "If you have no information to add, still write the SESSION GOAL section."
                        ),
                    })
                    continue  # one more iteration
                logger.info(
                    "curator_empty_final",
                    session_id=session_id,
                    tool_calls=total_tool_calls,
                )
                _save_failure_diagnostic(session_id, "empty_final", messages,
                    total_tool_calls, curated_paths, retained_turn_numbers, iteration)
                return None, tool_log, "empty_final"
            return CuratorResult(
                context_text=content,
                curated_paths=curated_paths,
                retained_turn_numbers=retained_turn_numbers,
                tool_calls_used=total_tool_calls,
                iterations_used=iterations_used,
                estimated_tokens=_estimate_tokens(content),
                tool_log=tool_log,
                prompt_tokens_used=curator_prompt_tokens,
                completion_tokens_used=curator_completion_tokens,
            ), tool_log, ""

        if choice.finish_reason == "length":
            logger.warning(
                "curator_context_length_exceeded",
                session_id=session_id,
                iteration=iteration + 1,
            )
            _save_failure_diagnostic(session_id, "context_length", messages,
                total_tool_calls, curated_paths, retained_turn_numbers, iteration)
            return None, tool_log, "context_length"

        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            logger.warning(
                "curator_unexpected_finish",
                session_id=session_id,
                finish_reason=choice.finish_reason,
                iteration=iteration + 1,
            )
            _save_failure_diagnostic(session_id, "unexpected_finish", messages,
                total_tool_calls, curated_paths, retained_turn_numbers, iteration,
                error_detail=f"finish_reason={choice.finish_reason}")
            return None, tool_log, f"unexpected_finish ({choice.finish_reason})"

        messages.append(choice.message)

        for tc in choice.message.tool_calls:
            tool_name = tc.function.name
            logger.debug("curator_tool_dispatch", tool=tool_name, session_id=session_id)
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as exc:
                result_str = "Error: invalid JSON arguments: " + str(tc.function.arguments)
                error_window.append((tool_name, "error"))
                tool_log.append(CuratorToolCall(tool=tool_name, args={}, status="error",
                    error=f"invalid arguments: {str(exc)[:200]}"))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
                continue

            if tool_name not in allowed_tools:
                result_str = "Error: unknown tool '" + tool_name + "'"
                tool_log.append(CuratorToolCall(tool=tool_name, args=args, status="error",
                    error="unknown tool"))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})
                continue

            try:
                handler = TOOL_HANDLERS[tool_name]
                result_str = await handler(session_id=session_id, **args)
                total_tool_calls += 1
                proxy_note = ""
                if tool_name in ("get_file", "get_file_lines", "prefetch_file"):
                    path = args.get("path", "")
                    if path:
                        if path in curated_paths:
                            proxy_note = (
                                "This path was already fetched earlier in this curator run. "
                                "Reuse the first result instead of fetching it again."
                            )
                            result_str += (
                                "\n\n(PROXY-NOTE: This path was already fetched earlier "
                                "in this curator run. The content above is identical to "
                                "the first result. Do NOT call get_file or get_file_lines "
                                "with this path again — reuse what you already have.)"
                            )
                            logger.info(
                                "curator_repeated_file_read",
                                path=path, session_id=session_id,
                                total_tool_calls=total_tool_calls,
                            )
                        else:
                            curated_paths.add(path)
                if tool_name in ("search_facts", "search_facts_semantic"):
                    query = args.get("query", "")
                    if query:
                        if query in _seen_queries:
                            proxy_note = (
                                "This exact query was already searched earlier in this curator run. "
                                "Reuse the first result instead of searching again."
                            )
                            result_str += (
                                "\n\n(PROXY-NOTE: This exact query was already searched "
                                "earlier in this curator run. Do NOT search again — use "
                                "the results from the first call.)"
                            )
                            logger.info(
                                "curator_repeated_search",
                                query=query[:60], session_id=session_id,
                            )
                        else:
                            _seen_queries.add(query)
                # Detect soft errors — tool returned but with an error/empty indicator
                is_soft_error = result_str.startswith("(") and ("not cached" in result_str or "no outline" in result_str or "no lines" in result_str)
                if is_soft_error:
                    tool_log.append(CuratorToolCall(tool=tool_name, args=args, status="soft_error",
                        error=result_str[:200], result_preview=result_str[:200],
                        raw_result=result_str, proxy_note=proxy_note))
                    logger.info("curator_tool_soft_error", tool=tool_name,
                        session_id=session_id, result=result_str[:200])
                else:
                    tool_log.append(CuratorToolCall(tool=tool_name, args=args, status="ok",
                        result_preview=result_str[:200], raw_result=result_str,
                        proxy_note=proxy_note))
                if tool_name == "select_relevant_turns":
                    turn_nums = args.get("turn_numbers", [])
                    retained_turn_numbers = [int(n) for n in turn_nums] if turn_nums else []
                    logger.info(
                        "curator_turn_selection",
                        session_id=session_id,
                        retained=retained_turn_numbers,
                    )
                error_window.append((tool_name, "ok"))
            except Exception as exc:
                result_str = "Error: " + type(exc).__name__ + ": " + str(exc)
                error_window.append((tool_name, "error"))
                tool_log.append(CuratorToolCall(tool=tool_name, args=args, status="error",
                    error=str(exc)[:200]))
                logger.warning(
                    "curator_tool_failed",
                    tool=tool_name,
                    session_id=session_id,
                    error=str(exc),
                )

            # Stuck-loop detection
            if len(error_window) >= _ERROR_WINDOW_SIZE:
                recent = error_window[-_ERROR_WINDOW_SIZE:]
                if all(e[1] == "error" for e in recent) and len(set(e[0] for e in recent)) == 1:
                    logger.warning("curator_stuck_loop", tool=recent[0][0], session_id=session_id)
                    _save_failure_diagnostic(session_id, "stuck_loop", messages,
                        total_tool_calls, curated_paths, retained_turn_numbers, iteration,
                        error_detail=f"stuck_on={recent[0][0]}")
                    return None, tool_log, f"stuck_loop ({recent[0][0]})"

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_str})

    logger.info("curator_max_iterations", session_id=session_id, iterations=max_iterations)
    _save_failure_diagnostic(session_id, "max_iterations", messages,
        total_tool_calls, curated_paths, retained_turn_numbers, max_iterations - 1)
    return None, tool_log, f"max_iterations ({max_iterations})"


__all__ = ["_run_curator_native"]
