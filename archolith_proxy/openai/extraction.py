"""Extraction functions — fact extraction, embeddings, and outline building."""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog

from archolith_proxy.config import get_settings
from archolith_proxy.extractor.client import extract_facts, extract_facts_per_tool
from archolith_proxy.graph.backend import get_backend
from archolith_proxy.metrics import record_metric
from archolith_proxy.models.graph_nodes import FactType, FileStatus
from archolith_proxy.openai.helpers import (
    _collect_recent_tool_results,
    _collect_tool_call_records,
    _extract_file_reads,
    _normalize_message_content,
)
from archolith_proxy.proxy.live import broadcast_extraction, broadcast_session_event
from archolith_proxy.proxy.rewrite import strip_reasoning
from archolith_proxy.trace.builder import TraceBuilder

logger = structlog.get_logger()


# ── _run_extraction ────────────────────────────────────────────────────────


async def _run_extraction(
    client,
    session_id: str,
    turn_number: int,
    messages: list[dict],
    response_text: str,
    truncated: bool = False,
    session_goal: str | None = None,
    trace_builder: TraceBuilder | None = None,
    promotion_service: object | None = None,
    is_user_turn: bool = True,
    response_finish_reason: str | None = None,
) -> None:
    """Run fact extraction and store results in graph. Best-effort, non-blocking."""
    from archolith_proxy.proxy.locks import get_session_lock

    lock = get_session_lock(session_id)
    acquired = False
    try:
        await asyncio.wait_for(lock.acquire(), timeout=10.0)
        acquired = True
    except asyncio.TimeoutError:
        logger.warning(
            "extraction_lock_acquire_timeout",
            session_id=session_id,
            turn=turn_number,
            note="skipping extraction — fail closed",
        )
        return

    try:
        user_message = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_message = _normalize_message_content(msg.get("content"))
                break

        response_text = _normalize_message_content(response_text)
        if not user_message and not response_text:
            return

        response_text = strip_reasoning(response_text)

        fc_settings = get_settings()
        if fc_settings.file_cache_enabled:
            from archolith_proxy.openai.file_cache import (
                _extract_file_writes, _invalidate_file_cache,
                _invalidate_written_files, _upsert_file_cache,
            )
            try:
                written_paths = _invalidate_written_files(messages)
                if written_paths:
                    await _invalidate_file_cache(session_id, written_paths, turn_number)

                file_reads = _extract_file_reads(messages)
                logger.info(
                    "file_cache_extract_result",
                    session_id=session_id,
                    turn=turn_number,
                    found=len(file_reads),
                    paths=[fr["path"] for fr in file_reads],
                )
                if file_reads:
                    await _upsert_file_cache(session_id, file_reads, turn_number)
                    logger.info(
                        "file_cache_upserted",
                        session_id=session_id,
                        turn=turn_number,
                        count=len(file_reads),
                        paths=[fr["path"] for fr in file_reads],
                    )

                file_writes = _extract_file_writes(messages)
                if file_writes:
                    await _upsert_file_cache(session_id, file_writes, turn_number)
                    logger.info(
                        "file_cache_writes_captured",
                        session_id=session_id,
                        turn=turn_number,
                        count=len(file_writes),
                        paths=[fw["path"] for fw in file_writes],
                    )
            except Exception:
                logger.warning("file_cache_capture_failed", session_id=session_id, turn=turn_number, exc_info=True)

        extraction_settings = get_settings()
        extraction_start = time.monotonic()

        if extraction_settings.per_tool_extraction_enabled:
            from archolith_proxy.extractor.registry import get_registry as _get_extractor_registry

            tool_records = _collect_tool_call_records(messages)
            result = await extract_facts_per_tool(
                http_client=client,
                turn_number=turn_number,
                user_message=user_message[:4000],
                assistant_response=response_text[:8000],
                tool_records=tool_records,
                session_goal=session_goal,
                registry=_get_extractor_registry(),
            )
        else:
            tool_results = _collect_recent_tool_results(messages, max_chars=4000)
            result = await extract_facts(
                http_client=client,
                turn_number=turn_number,
                user_message=user_message[:4000],
                assistant_response=response_text[:8000],
                tool_results=tool_results,
                session_goal=session_goal,
            )
        extraction_latency_ms = (time.monotonic() - extraction_start) * 1000

        if result and result.session_goal:
            try:
                await get_backend().update_goal(session_id, result.session_goal)
                logger.info("session_goal_updated", session_id=session_id, goal=result.session_goal[:80])
                await broadcast_session_event(session_id, "goal_updated", goal=result.session_goal)
            except Exception as e:
                logger.warning("session_goal_update_failed", session_id=session_id, error=str(e))

        if not result:
            record_metric("extraction_failures", 1)
            logger.warning("extraction_result_missing", session_id=session_id, turn=turn_number)
            if trace_builder:
                trace_builder.set_extraction(extraction_latency_ms=extraction_latency_ms)
            return

        if not result.facts:
            logger.info("extraction_empty", session_id=session_id, turn=turn_number)
            record_metric("extraction_empties", 1)
            if trace_builder:
                trace_builder.set_extraction(extraction_latency_ms=extraction_latency_ms)
            return

        from archolith_proxy.extractor.dedup import deduplicate_facts as _deduplicate_facts
        _fact_limit = get_settings().fact_pool_limit
        existing_facts = await get_backend().get_active_facts(session_id, limit=_fact_limit)
        if len(existing_facts) >= _fact_limit:
            logger.warning(
                "fact_pool_at_capacity",
                session_id=session_id, turn=turn_number, limit=_fact_limit,
                msg="dedup may miss older facts beyond the pool limit",
            )
        unique_facts = _deduplicate_facts(result.facts, existing_facts)
        if len(unique_facts) < len(result.facts):
            logger.info(
                "extraction_dedup_applied",
                session_id=session_id, turn=turn_number,
                original=len(result.facts),
                after_dedup=len(unique_facts),
                duplicates_removed=len(result.facts) - len(unique_facts),
            )

        fact_contents = [fact.get("content", "") for fact in unique_facts]
        embeddings = await _compute_fact_embeddings(client, fact_contents)

        enriched_facts = []
        for i, fact in enumerate(unique_facts):
            fact_type_str = fact.get("fact_type", "observation")
            try:
                fact_type = FactType(fact_type_str)
            except ValueError:
                fact_type = FactType.OBSERVATION
            enriched_facts.append({
                "content": fact.get("content", ""),
                "fact_type": fact_type.value,
                "confidence": fact.get("confidence", 0.5),
                "embedding": embeddings[i] if i < len(embeddings) else None,
            })
        new_fact_ids = await get_backend().store_facts_batch(
            session_id=session_id, facts=enriched_facts, source_turn=turn_number,
        )

        # Bulk: single UNWIND for all file touches
        if result.files_touched:
            touched = [{"file_path": fp, "status": FileStatus.MODIFIED.value, "turn": turn_number}
                       for fp in result.files_touched]
            await get_backend().bulk_create_touches(session_id, touched)

        # Bulk: single UNWIND for all decisions
        if result.decisions:
            await get_backend().bulk_store_decisions(session_id, result.decisions, turn_number)

        if result.checkpoint:
            try:
                await get_backend().upsert_checkpoint(
                    session_id=session_id,
                    summary=result.checkpoint.get("summary", ""),
                    next_step=result.checkpoint.get("next_step", ""),
                    confidence=result.checkpoint.get("confidence", 0.5),
                    turn=turn_number,
                )
            except Exception as e:
                logger.warning("checkpoint_store_failed", session_id=session_id, error=str(e))

        if result.issues:
            open_issues = [i for i in result.issues if i.get("status") != "resolved"]
            resolved_issues = [i for i in result.issues if i.get("status") == "resolved"]
            # Bulk: single UNWIND for all open issues
            if open_issues:
                try:
                    await get_backend().bulk_create_issues(session_id, open_issues, turn_number)
                except Exception as e:
                    logger.warning("bulk_issue_store_failed", session_id=session_id, error=str(e))
            if resolved_issues:
                try:
                    summaries = [i.get("summary", "") for i in resolved_issues if i.get("summary")]
                    if summaries:
                        await get_backend().bulk_resolve_issues(
                            session_id, summaries,
                            f"resolved at turn {turn_number}", turn_number,
                        )
                except Exception as e:
                    logger.warning("bulk_issue_resolve_failed", session_id=session_id, error=str(e))

        # Bulk: single UNWIND for all verifications
        if result.verifications:
            try:
                await get_backend().bulk_create_verifications(session_id, result.verifications, turn_number)
            except Exception as e:
                logger.warning("bulk_verification_store_failed", session_id=session_id, error=str(e))

        invalidations_matched_count = 0
        if result.invalidated_fact_ids:
            matched_ids = await get_backend().find_matching_fact_ids(
                session_id, result.invalidated_fact_ids
            )
            invalidations_matched_count = len(matched_ids)
            if matched_ids:
                count = await get_backend().invalidate_facts(matched_ids)
                if count:
                    logger.info(
                        "facts_invalidated",
                        count=count, session_id=session_id, turn=turn_number,
                        descriptions=len(result.invalidated_fact_ids),
                        matched_ids=len(matched_ids),
                    )
                    # Bulk: single UNWIND for all supersede edges
                    pairs = [(old_fid, new_fid) for new_fid in new_fact_ids for old_fid in matched_ids]
                    try:
                        await get_backend().bulk_create_supersedes(pairs)
                    except Exception as e:
                        logger.warning(
                            "bulk_supersedes_failed",
                            session_id=session_id, pairs=len(pairs), error=str(e),
                        )
                    logger.debug(
                        "supersedes_edges_created",
                        session_id=session_id, new_facts=len(new_fact_ids),
                        invalidated=len(matched_ids),
                        edges=len(pairs),
                    )

        active_count = await get_backend().get_active_fact_count(session_id)
        embedding_count = sum(1 for e in embeddings if e is not None)
        record_metric("extraction_successes", 1)
        logger.info(
            "extraction_stored",
            session_id=session_id, turn=turn_number,
            facts_stored=len(unique_facts),
            embeddings_computed=embedding_count,
            active_fact_count=active_count,
            extraction_latency_ms=round(extraction_latency_ms, 1),
            warning="high_active_count" if active_count > 200 else None,
        )

        await broadcast_extraction(
            session_id=session_id, turn_number=turn_number,
            facts_stored=len(unique_facts),
            session_goal=result.session_goal,
            latency_ms=extraction_latency_ms,
        )

        if trace_builder:
            duplicates_skipped = len(result.facts) - len(unique_facts)
            trace_builder.set_extraction(
                facts_stored=len(unique_facts),
                duplicates_skipped=duplicates_skipped,
                invalidations_attempted=len(result.invalidated_fact_ids) if result.invalidated_fact_ids else 0,
                invalidations_matched=invalidations_matched_count,
                extraction_latency_ms=extraction_latency_ms,
                extracted_facts=[{"content": f.get("content", "")[:200], "type": f.get("fact_type", "observation")} for f in unique_facts],
            )

        if promotion_service is not None:
            try:
                from archolith_proxy.memory.models import PromotionRecord

                svc = promotion_service
                settings = get_settings()

                if settings.promotion_enabled:
                    eligible_records = []
                    for fact in unique_facts:
                        fact_type = fact.get("fact_type", "observation")
                        confidence = fact.get("confidence", 0.5)
                        if svc.should_promote(
                            fact_type=fact_type, confidence=confidence,
                            tags=fact.get("tags", []),
                        ):
                            record = PromotionRecord(
                                session_id=session_id,
                                source_turn=turn_number,
                                fact_type=fact_type,
                                content=fact.get("content", ""),
                                confidence=confidence,
                                session_goal=session_goal,
                                touched_files=result.files_touched if hasattr(result, "files_touched") else [],
                                promotion_reason="auto_extracted",
                                tags=fact.get("tags", []),
                            )
                            eligible_records.append(record)

                    if eligible_records:
                        record_metric("promotions_attempted", len(eligible_records))
                        promo_results = await svc.promote_batch(
                            eligible_records, dry_run=settings.promotion_dry_run,
                        )
                        succeeded = sum(1 for r in promo_results if r.outcome.value == "success")
                        skipped = sum(1 for r in promo_results if r.outcome.value == "skipped")
                        failed = sum(1 for r in promo_results if r.outcome.value == "failed")
                        record_metric("promotions_succeeded", succeeded)
                        record_metric("promotions_failed", failed)
                        record_metric("promotions_skipped", skipped)
                        logger.info(
                            "promotion_completed",
                            session_id=session_id, turn=turn_number,
                            eligible=len(eligible_records),
                            succeeded=succeeded, skipped=skipped, failed=failed,
                        )
            except Exception as e:
                logger.warning("promotion_task_failed", session_id=session_id, turn=turn_number, error=str(e), exc_info=True)

    except Exception as e:
        record_metric("extraction_failures", 1)
        logger.warning("extraction_task_failed", session_id=session_id, turn=turn_number, error=str(e), exc_info=True)
    finally:
        if acquired:
            lock.release()

    if not is_user_turn or response_finish_reason == "tool_calls":
        return

    try:
        _bg_settings = get_settings()
        if _bg_settings.background_pass_enabled and _bg_settings.curator_enabled and session_id:
            _bg_user_msg = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    _bg_user_msg = _normalize_message_content(msg.get("content"))
                    break
            if _bg_user_msg:
                from archolith_proxy.curator import run_background_pass
                from archolith_proxy.curator.state import swap_background_task
                _bg_task = asyncio.create_task(
                    run_background_pass(
                        session_id=session_id,
                        turn_number=turn_number,
                        user_message=_bg_user_msg[:4000],
                        session_goal=session_goal,
                        messages=messages,
                    )
                )
                swap_background_task(session_id, _bg_task)
                logger.debug("background_pass_triggered", session_id=session_id, turn=turn_number)
    except Exception:
        logger.debug("background_pass_trigger_failed", session_id=session_id, exc_info=True)


async def _compute_fact_embeddings(
    client: httpx.AsyncClient,
    texts: list[str],
) -> list[list[float] | None]:
    """Compute batch embeddings for extracted fact texts."""
    try:
        from archolith_proxy.extractor.embeddings import compute_embeddings_batch
        return await compute_embeddings_batch(client, texts)
    except Exception as e:
        logger.warning("embedding_computation_failed", error=str(e))
        return [None] * len(texts)
