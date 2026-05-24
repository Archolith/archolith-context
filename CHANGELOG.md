# Changelog

## Unreleased

- Updated `.agent/architecture.md` to document `archolith-context` as the new project name, the Curator LLM subsystem (entry point, loop, 7 tools, result type), File Content Cache (FileContent schema, SHA-256 dedup pipeline), updated data flow with curator-then-heuristic path, new env vars (`CURATOR_*`, `FILE_CACHE_*`), and curator metrics counters. Removed RTK references (RTK belongs in `archolith-rtk`).

## 2026-05-23 — Phases 1–4: File Content Cache + LLM-driven Curator

- Added `archolith_proxy/curator/` package: `CuratorResult` dataclass, 7 async curator tools, OpenAI-compatible tool schemas, system prompt, LLM loop (`_run_curator_native` + Nous XML fallback, ported from cth.mcp.delegate), `curate_context()` entry point with `asyncio.wait_for` 6s hard cap and heuristic fallback.
- Added `FileContent` LadybugDB node table with SHA-256 dedup; `_extract_file_reads()` pairs file-read tool results to calls via `tool_call_id`; `_upsert_file_cache()` called inside `_run_extraction()` before message flattening.
- Wired curator as primary assembly path in `chat.py`; heuristic assembler used as fallback when curator returns `None` or is disabled.
- Added `FILE_CACHE_ENABLED`, `FILE_CACHE_MAX_FILE_BYTES`, `CURATOR_*` settings to `config.py`.
- Added `curator_calls`, `curator_timeouts`, `curator_fallbacks`, `assembly_modes["curator"]` to `metrics.py`.
- Fixed streaming trace finalization so `facts_stored`, `extracted_facts`, and `upstream_response_summary` are recorded after post-response extraction instead of being frozen at zero before the background task runs.
- Fixed extraction input shaping to normalize structured content blocks and prioritize the newest tool outputs within the 4K extraction budget instead of truncating from the oldest tool results first.
- Added `extraction_empties` to `/metrics` and stopped counting zero-fact parses as successful extractions.
- Fixed `/trace/qa/extract` dedup and invalidation diagnostics to use the active graph backend instead of Neo4j-only helpers.
- Added regression coverage for streaming trace ordering, recent-tool extraction prompts, and empty-extraction metric semantics.
- Fixed recall tool formatting so `__archolith_recall` now emits string `role="tool"` content instead of serializing the `(text, compression_ratio)` tuple from `_format_relevant_facts()`.
- Fixed compaction re-write flow so compacted context updates the actual outbound payload, not just `AssembledContext.system_message`.
- Fixed skipped-rewrite trace accounting so `rewritten_messages`, `rewritten_tokens`, and `savings_tokens` reflect the payload actually sent upstream.
- Added regression coverage for recall formatting, compaction-aware rewriting, skipped-low-tokens trace accounting, and the streaming recall test warning.
- Updated project architecture docs to point at the canonical `archolith_proxy/` package tree and current bootstrap defaults.
- Added optional Phase 4 RTK integration for outbound tool-role messages via `RTK_ENABLED` and a fail-open proxy adapter.
- Applied RTK filtering to the primary upstream request path plus recall re-send paths so surviving tool results are filtered consistently.
- Added proxy coverage for RTK-enabled and RTK-disabled outbound payload behavior and documented the new configuration surface.
