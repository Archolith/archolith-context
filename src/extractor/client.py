"""gpt-4.1-mini fact extraction client."""

from __future__ import annotations

import json

import httpx
import structlog

from src.config import get_settings
from src.extractor.prompts import SYSTEM_PROMPT, build_extraction_prompt
from src.models.dtos import ExtractionResult

logger = structlog.get_logger()


async def extract_facts(
    http_client: httpx.AsyncClient,
    turn_number: int,
    user_message: str,
    assistant_response: str,
    tool_results: str | None = None,
    session_goal: str | None = None,
) -> ExtractionResult | None:
    """Call gpt-4.1-mini to extract facts from a turn.

    Returns None if extraction fails (best-effort, non-blocking).
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

        logger.info(
            "extraction_complete",
            turn=turn_number,
            facts=len(parsed.facts),
            files=len(parsed.files_touched),
            decisions=len(parsed.decisions),
            invalidated=len(parsed.invalidated_fact_ids),
        )
        return parsed

    except Exception as e:
        logger.warning("extraction_failed", turn=turn_number, error=str(e))
        return None


def _parse_extraction_response(content: str, turn_number: int) -> ExtractionResult:
    """Parse the extraction model's JSON response."""
    # Strip markdown code fences if present
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (code fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("extraction_parse_error", content=content[:200])
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

    return ExtractionResult(
        facts=facts,
        files_touched=files_touched,
        decisions=decisions,
        invalidated_fact_ids=invalidated_ids,
        turn_number=turn_number,
        session_goal=session_goal,
    )
