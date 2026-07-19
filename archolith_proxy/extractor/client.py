"""gpt-4.1-mini fact extraction client."""

from __future__ import annotations

import asyncio
import json

import httpx
import structlog

from archolith_proxy.extractor.budget import (
    ExtractionBudget, LLMBudgetExceeded, set_budget, reset_budget,
)

from archolith_proxy.config import get_settings
from archolith_proxy.compliance import redact_for_log
from archolith_proxy.extractor.base import PartialExtractionResult, ToolCallRecord
from archolith_proxy.extractor.dedup import deduplicate_facts
from archolith_proxy.extractor.prompts import (
    SYSTEM_PROMPT,
    TURN_LEVEL_SYSTEM_PROMPT,
    build_extraction_prompt,
    build_turn_level_extraction_prompt,
)
from archolith_proxy.extractor.registry import ToolExtractorRegistry, get_registry
from archolith_proxy.models.dtos import ExtractionResult

logger = structlog.get_logger()

__all__ = [
    "extract_facts",
    "extract_facts_per_tool",
]


async def extract_facts(
    http_client: httpx.AsyncClient,
    turn_number: int,
    user_message: str,
    assistant_response: str,
    tool_results: str | None = None,
    session_goal: str | None = None,
) -> ExtractionResult:
    """Call gpt-4.1-mini to extract facts from a turn.

    Returns ExtractionResult with empty facts/files/decisions if extraction fails
    (best-effort, non-blocking).
    """
    settings = get_settings()

    user_prompt = build_extraction_prompt(
        turn_number=turn_number,
        user_message=user_message,
        assistant_response=assistant_response,
        tool_results=tool_results,
        session_goal=session_goal,
    )

    payload = {
        "model": settings.extractor_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }

    try:
        resp = await http_client.post(
            f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
        headers={
                "Authorization": f"Bearer {settings.extractor_api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload).encode(),
        )
        resp.raise_for_status()
        data = resp.json()

        content = data["choices"][0]["message"]["content"]
        parsed = _parse_extraction_response(content, turn_number)

        # Capture LLM usage from upstream response
        usage = data.get("usage", {})
        if usage:
            parsed.usage = {
                "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
                "completion_tokens": usage.get("completion_tokens", 0) or 0,
                "llm_calls": 1,
                "cached_tokens": (usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0,
            }

        logger.info(
            "extraction_complete",
            turn=turn_number,
            facts=len(parsed.facts),
            files=len(parsed.files_touched),
            decisions=len(parsed.decisions),
            invalidated=len(parsed.invalidated_fact_ids),
            usage=parsed.usage,
        )
        return parsed

    except Exception as e:
        logger.warning("extraction_failed", turn=turn_number, error=str(e))
        return ExtractionResult(
            facts=[], files_touched=[], decisions=[],
            invalidated_fact_ids=[], turn_number=turn_number,
        )


def _parse_extraction_response(
    content: str,
    turn_number: int,
    source: str | None = None,
) -> ExtractionResult:
    """Parse the extraction model's JSON response.

    Args:
        content: The JSON or markdown-fenced JSON response from the LLM.
        turn_number: Current turn number for logging.
        source: Optional source label (e.g. "Turn-level") to prefix facts with.
                If provided, facts will be prefixed like "[Turn-level] fact content".
    """
    # Strip markdown code fences if present
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (code fences)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("extraction_parse_error", content=redact_for_log(content))
        return ExtractionResult(
            facts=[], files_touched=[], decisions=[],
            invalidated_fact_ids=[], turn_number=turn_number,
        )

    facts = data.get("facts", [])
    # Normalize: model sometimes returns bare strings instead of dicts
    normalized_facts = []
    for f in facts:
        if isinstance(f, str):
            normalized_facts.append({"content": f, "fact_type": "observation", "confidence": 0.5})
        elif isinstance(f, dict):
            normalized_facts.append(f)
    facts = normalized_facts

    # Add provenance prefix if source is provided
    if source:
        for f in facts:
            content_str = f.get("content", "")
            f["content"] = f"[{source}] {content_str}"

    # Normalize files_touched: model may return bare strings, or dicts with "path"/"file" keys
    raw_files = data.get("files_touched", [])
    normalized_files = []
    for f in raw_files:
        if isinstance(f, str):
            normalized_files.append(f)
        elif isinstance(f, dict):
            # Accept both "path" and "file" keys
            path = f.get("path") or f.get("file") or f.get("name") or ""
            if path:
                normalized_files.append(path)
    files_touched = normalized_files

    # Normalize decisions: model may return bare strings instead of dicts
    decisions = []
    for d in data.get("decisions", []):
        if isinstance(d, str):
            decisions.append({"summary": d})
        elif isinstance(d, dict):
            decisions.append(d)

    # Collect invalidated fact descriptions for matching
    invalidated = data.get("invalidated", [])
    invalidated_ids = []
    if isinstance(invalidated, list):
        for inv in invalidated:
            if isinstance(inv, str):
                invalidated_ids.append(inv)

    # Extract session goal
    session_goal = data.get("session_goal")
    if not isinstance(session_goal, str):
        session_goal = None

    # Extract checkpoint
    checkpoint = None
    raw_checkpoint = data.get("checkpoint")
    if isinstance(raw_checkpoint, dict) and raw_checkpoint.get("summary"):
        conf = raw_checkpoint.get("confidence", 0.5)
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.5
        checkpoint = {
            "summary": str(raw_checkpoint.get("summary", "")),
            "next_step": str(raw_checkpoint.get("next_step") or ""),
            "confidence": max(0.0, min(1.0, conf)),
        }

    # Extract issues
    issues = []
    for item in (data.get("issues") or []):
        if not isinstance(item, dict):
            continue
        summary = item.get("summary", "")
        if not summary:
            continue
        status = item.get("status", "open")
        if status not in ("open", "resolved"):
            status = "open"
        issues.append({
            "summary": str(summary),
            "status": status,
            "related_file": str(item.get("related_file") or ""),
            "related_command": str(item.get("related_command") or ""),
        })

    # Extract verifications
    verifications = []
    for item in (data.get("verifications") or []):
        if not isinstance(item, dict):
            continue
        command = item.get("command", "")
        if not command:
            continue
        status = item.get("status", "fail")
        if status not in ("pass", "fail", "partial"):
            status = "fail"
        verifications.append({
            "command": str(command),
            "status": status,
            "summary": str(item.get("summary") or ""),
        })

    return ExtractionResult(
        facts=facts,
        files_touched=files_touched,
        decisions=decisions,
        invalidated_fact_ids=invalidated_ids,
        turn_number=turn_number,
        session_goal=session_goal,
        checkpoint=checkpoint,
        issues=issues,
        verifications=verifications,
    )


# ---------------------------------------------------------------------------
# Per-tool extraction orchestrator
# ---------------------------------------------------------------------------

_llm_semaphore: asyncio.Semaphore | None = None
_TURN_LEVEL_MAX_TOKENS = 2000


def _int_setting(settings, name: str, default: int) -> int:
    """Read additive settings without breaking lightweight test/embedded settings."""
    try:
        value = getattr(settings, name, default)
        if not isinstance(value, int):
            return default
        return value
    except (TypeError, ValueError):
        return default


def _get_llm_semaphore() -> asyncio.Semaphore:
    """Lazy-init semaphore for LLM-backed extractor concurrency control.

    The semaphore is built once to match the configured extractor_llm_concurrency.
    Call _reset_llm_semaphore() if the concurrency setting changes or to rebuild.
    """
    global _llm_semaphore
    if _llm_semaphore is None:
        settings = get_settings()
        _llm_semaphore = asyncio.Semaphore(settings.extractor_llm_concurrency)
    return _llm_semaphore


def _reset_llm_semaphore() -> None:
    """Reset the global LLM semaphore (for testing or config changes).

    Useful when the configured extractor_llm_concurrency changes and you need
    to rebuild the semaphore with the new value.
    """
    global _llm_semaphore
    _llm_semaphore = None


async def _extract_with_semaphore(
    extractor,
    record: ToolCallRecord,
    http_client: httpx.AsyncClient,
    turn_number: int,
    session_goal: str | None,
) -> PartialExtractionResult:
    """Run an extractor's extract() — gating behind the LLM semaphore only when needed.

    Extractors that declare ``may_use_llm = True`` (BashExtractor, WebFetchExtractor,
    DefaultExtractor) are gated behind the concurrency semaphore so they don't
    exhaust API quota in parallel. Pure no-LLM extractors (Grep, Glob, LS, Find,
    Read, WriteEdit, WebSearch, MemoryRecall) run without the semaphore and can
    proceed fully concurrently.
    Budget reservation happens here (orchestrator level) using the extractor's
    declared llm_requested_tokens.
    """
    if extractor is None:
        # A custom registry must not be able to discard a tool result. The
        # built-in registry always has a default extractor; this guard protects
        # embedded callers that supply a partial registry.
        extractor = get_registry().get(record.tool_name)
        if extractor is None:
            raise LookupError(f"No extractor available for {record.tool_name!r}")
    if extractor.may_use_llm:
        tokens = getattr(extractor, "llm_requested_tokens", 0)
        if not isinstance(tokens, int) or tokens <= 0:
            # Safe policy for custom/undeclared LLM extractors: return deterministic fallback
            # without making an upstream call and without bypassing budget.
            return PartialExtractionResult(
                source_tool=getattr(record, "tool_name", "unknown"),
                facts=[{"content": f"[{record.tool_name}] (budget policy) limited result", "fact_type": "observation", "confidence": 0.1}],
                files_touched=[],
                used_llm=False,
            )
        from archolith_proxy.extractor.budget import reserve_llm_call
        try:
            reserve_llm_call(tokens)
        except Exception:
            # Budget exhausted — return safe deterministic fallback, no HTTP call
            return PartialExtractionResult(
                source_tool=record.tool_name,
                facts=[{"content": f"[{record.tool_name}] (budget exhausted) limited result", "fact_type": "observation", "confidence": 0.1}],
                files_touched=[],
                used_llm=False,
            )
        sem = _get_llm_semaphore()
        async with sem:
            return await extractor.extract(record, http_client, turn_number, session_goal)
    return await extractor.extract(record, http_client, turn_number, session_goal)


async def extract_facts_per_tool(
    http_client: httpx.AsyncClient,
    turn_number: int,
    user_message: str,
    assistant_response: str,
    tool_records: list[ToolCallRecord],
    session_goal: str | None = None,
    registry: ToolExtractorRegistry | None = None,
) -> ExtractionResult | None:
    """Per-tool extraction: fan out to specialized extractors, then run turn-level LLM.

    1. Fan out all .extract() calls concurrently with semaphore-capped LLM calls.
    2. Merge partial results with explicit exception guard.
    3. Run one turn-level LLM call for decisions, checkpoint, issues, etc.
    4. Merge turn-level results with per-tool results. Dedup by content hash.
    5. Return combined ExtractionResult.
    """
    if registry is None:
        registry = get_registry()

    # Step 1: Fan out per-tool extractors concurrently under one shared
    # per-turn budget. This bounds cost without limiting deterministic parsing.
    settings = get_settings()
    budget = ExtractionBudget(
        max_llm_calls=max(0, _int_setting(settings, "extractor_llm_max_calls_per_turn", 4)),
        max_requested_tokens=max(0, _int_setting(
            settings, "extractor_llm_max_requested_tokens_per_turn", 5000,
        )),
    )
    budget_token = set_budget(budget)
    try:
        # The turn-level pass is responsible for decisions, checkpoints, issues,
        # and verifications. Reserve its capacity before concurrent tool fallbacks
        # compete for the shared budget, so a tool-heavy turn cannot starve it.
        turn_level_reserved = budget.reserve(_TURN_LEVEL_MAX_TOKENS)
        partial_results = await asyncio.gather(
            *[
                _extract_with_semaphore(
                    registry.get(r.tool_name), r, http_client, turn_number, session_goal
                )
                for r in tool_records
            ],
            return_exceptions=True,
        )

        # Step 2: Merge partial results with exception guard
        all_facts: list[dict] = []
    all_files: list[str] = []
    llm_calls_made = 0
    usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "cached_tokens": 0}
    for r in partial_results:
        if isinstance(r, Exception):
            logger.warning("per_tool_extractor_failed", error=str(r))
            continue
        for fact in (r.facts or []):
            # Preserve the extractor-level provenance even when an individual
            # extractor omitted it. This is the single merge point before graph
            # persistence.
            if isinstance(fact, dict):
                fact.setdefault("source_tool", r.source_tool)
        all_facts.extend(r.facts or [])
        all_files.extend(r.files_touched or [])
        if r.used_llm:
            calls = r.usage.get("llm_calls", 1) or 1
            llm_calls_made += calls
            usage["llm_calls"] += calls
            usage["prompt_tokens"] += r.usage.get("prompt_tokens", 0) or 0
            usage["completion_tokens"] += r.usage.get("completion_tokens", 0) or 0
            usage["cached_tokens"] += r.usage.get("cached_tokens", 0) or 0

    logger.info(
        "per_tool_extraction_gathered",
        turn=turn_number,
        records=len(tool_records),
        facts=len(all_facts),
        files=len(all_files),
        llm_calls=llm_calls_made,
        failures=sum(1 for r in partial_results if isinstance(r, Exception)),
    )

    # Step 3: Run turn-level LLM call for decisions, checkpoint, issues, verifications
    _turn_level_facts_count = 0  # used for logging; avoids fragile "turn_result" in dir() guard
    settings = get_settings()
    turn_level_prompt = build_turn_level_extraction_prompt(
        turn_number=turn_number,
        user_message=user_message[:4000],
        assistant_response=assistant_response[:8000],
        session_goal=session_goal,
    )

    turn_payload = {
        "model": settings.extractor_model,
        "messages": [
            {"role": "system", "content": TURN_LEVEL_SYSTEM_PROMPT},
            {"role": "user", "content": turn_level_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": _TURN_LEVEL_MAX_TOKENS,
    }

    try:
        if not turn_level_reserved:
            raise LLMBudgetExceeded("per-turn extractor LLM budget exhausted")
        resp = await http_client.post(
            f"{settings.extractor_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.extractor_api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(turn_payload).encode(),
        )
        resp.raise_for_status()
        data = resp.json()
        turn_content = data["choices"][0]["message"]["content"]
        turn_result = _parse_extraction_response(turn_content, turn_number, source="Turn-level")
        _turn_level_facts_count = len(turn_result.facts)

        # Capture turn-level usage from upstream response
        turn_usage = data.get("usage", {})
        if turn_usage:
            usage["prompt_tokens"] += turn_usage.get("prompt_tokens", 0) or 0
            usage["completion_tokens"] += turn_usage.get("completion_tokens", 0) or 0
            usage["cached_tokens"] += (turn_usage.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0
        usage["llm_calls"] += 1
        turn_result.usage = usage.copy()

        # Step 4: Merge — add turn-level facts that don't duplicate per-tool facts.
        # Use Jaccard near-duplicate check (not just exact MD5) to catch similar facts.
        merged_facts = deduplicate_facts(turn_result.facts, all_facts)
        all_facts.extend(merged_facts)

        # Merge files
        existing_paths = set(all_files)
        for p in turn_result.files_touched:
            if p not in existing_paths:
                all_files.append(p)
                existing_paths.add(p)

        # Turn-level contributes decisions, checkpoint, issues, verifications
        decisions = turn_result.decisions
        checkpoint = turn_result.checkpoint
        issues = turn_result.issues
        verifications = turn_result.verifications
        session_goal_result = turn_result.session_goal
        invalidated = turn_result.invalidated_fact_ids

    except Exception as e:
        logger.warning("turn_level_extraction_failed", turn=turn_number, error=str(e))
        # Fall back to whatever per-tool gave us
        decisions = []
        checkpoint = None
        issues = []
        verifications = []
        session_goal_result = session_goal
        invalidated = []

    logger.info(
        "per_tool_extraction_complete",
        turn=turn_number,
        total_facts=len(all_facts),
        per_tool_facts=len(all_facts) - _turn_level_facts_count,
        turn_level_facts=_turn_level_facts_count,
        files=len(all_files),
        decisions=len(decisions),
    )

    return ExtractionResult(
        facts=all_facts,
        files_touched=all_files,
        decisions=decisions,
        invalidated_fact_ids=invalidated,
        turn_number=turn_number,
        session_goal=session_goal_result,
        checkpoint=checkpoint,
        issues=issues,
        verifications=verifications,
        usage=usage,
    )
    finally:
        reset_budget(budget_token)
