# Changelog

## 2026-06-07 — Chunk 7: Cross-cutting hygiene and version metadata

Port and version standardization, docker/pyproject hardening, optional javalang:
- **Port alignment:** All scripts default to 9800 (canonical port matching config.py and docker-compose.yml). Deployment must migrate from 9801 to 9800.
- **Version metadata:** `__version__` now read from installed package (pyproject 0.3.0) via `importlib.metadata`, with "0.0.0-dev" fallback. Replaces hardcoded "0.1.0" in main.py health endpoints + metrics_router.py.
- **Dockerfile:** Copies `uv.lock` for reproducible builds; installs production deps only (no dev); removes test/ from runtime image.
- **pyproject.toml:** Removed `httpx` duplication from dev deps (already in main). Moved `javalang` to optional `[project.optional-dependencies]` `java = [...]`.
- **text_utils.py:** `_build_outline()` now catches `ImportError` for missing javalang, gracefully falling back to regex-based outline.
- **Trace retention:** Added `trace_retention_days` config setting (default 0 = no cleanup); TraceStore now runs cleanup on startup to delete JSONL files older than the retention window.
- **gitignore:** Added `.ruff_cache/` and `config_overrides.json` (runtime override file, not for repo).
- **scripts/README.md:** Documented missing scripts (scripted_benchmark.py, harness_benchmark.py, test_synthetic_tools.py, redundancy.py, opencode_export.py).
- **docker-compose.yml:** Added EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL env vars.

## 2026-06-05 — Fix: proxy was inert on real sessions (RTK missing + trace mislabel)

Root-caused via replaying a real captured coding session: the proxy did NO context
management on real agent sessions (100% passthrough), which produced the 58/100 grade.
- **Root cause (env):** `archolith_rtk` was installed only in the global Python, not in
  the proxy's `.venv` (which `proxy_restart.py` launches). Agent-solo compression is RTK
  code, so it silently no-op'd and `rtk_available` was false. Fixed by installing RTK into
  the venv; after the fix a real session compresses ~190K chars/turn (filter+dedup+shrink).
- `main.py`: **refuse to start** (raise `RuntimeError`) when `RTK_ENABLED=true` but
  `archolith_rtk` is not importable — a proxy that silently does no curation is worse than
  one that won't boot. (`rtk_enabled` defaults to false, so tests are unaffected.)
- `openai/chat.py`: **`set_assembly()` is now called on the normal request path.** It only
  ran in the `-passthrough` branch before, so every normal request recorded
  `assembly_mode="passthrough"` with 0 savings even when agent-solo/curator compressed
  heavily — the reason every baseline looked 100% passthrough. Traces now report the real
  mode and savings.
- `scripts/benchmark.py`: pin the proxy session via `X-Session-ID` (`send_chat` gains a
  `session_id` arg); trace lookups no longer guess `sessions[0]` and grab a stale
  disk-restored session.

## 2026-06-05 — Proxy memory-leak fixes (recoverable on session resume)

Investigated unbounded in-memory growth before running the tuning baseline. Fixes below;
all evicted/pruned state is recoverable — caches rebuild from the graph or on the next turn.
- `trace/store.py`: `_bg_passes` is now capped per session (`max_bg_passes_per_session`, default 50) like turns were, and session LRU eviction now drops `_bg_passes` + `_session_meta` (previously only turns were dropped, leaking both for every evicted session). Added `has_session_metadata()`.
- `openai/chat.py`: per-session trace metadata (harness_env, proxy_config) now repopulates whenever absent — not only on `is_new` — so a session resuming after an LRU eviction restores its metadata instead of losing it for the process lifetime.
- `main.py`: the in-memory cache prune (curator + agent-solo + last-attempts) now runs every cleanup cycle, not only when a graph session expired — previously the prune was nested under `if expired:` and could be skipped indefinitely.
- `curator/pipeline.py`: added `prune_last_attempts()` for the `_last_attempt` diagnostic map (regenerated per curator run).
- `proxy/agent_solo.py`: `_curator_caches` (holds full rewritten message lists) now has a hard cap mirroring `_session_trackers`, as defense-in-depth between prune cycles.
- `tests/test_memory_bounds.py`: regression tests for the bg-pass cap, eviction cleanup, resume recoverability, last-attempt prune, and curator-cache cap.

## 2026-06-05 — RTK / curator tuning: Step 0 offline harness extensions

- `scripts/redundancy.py`: offline read-file redundancy analyzer — classifies file-read tokens in a captured session into exact-dup / superseded-by-full-write / live buckets to size RTK Step 5-B and curator Step 4-C before building them. Partial edits do not count as superseding. 6 unit tests.
- `scripts/benchmark.py`: added `EditProbe` + pure `score_edit_probe` + `run_edit_probes` — an edit-fidelity probe alongside the keyword `FactProbe`. Fidelity = fraction of required fragments present, 0.0 if any forbidden (stale) fragment appears. Reports `avg_*_fidelity`, `fidelity_preservation`, `proxy_forbidden_hits`; wired into scenario loading, checkpoint/resume, summary, and print output. 7 unit tests.
- `.agent/workflows/benchmarking.md`: documented both tools and the determinism decision — variance-based (N>=3 runs, compare medians/spread) rather than a byte-replay cache, since the harness makes live temperature-0.3 calls.

## 2026-06-04 — RTK / curator tuning: instrumentation, proxy recall, and repeated-call detection (Steps 1–4)

### Step 1 — Baseline instrumentation
- Added `rtk_available`, `rtk_chars_saved`, `rtk_chars_before` to `TurnTrace` — records whether archolith_rtk is installed and how many chars the filter removed per turn.
- Added `curator_skip_reason` to `TurnTrace` — classifies why the curator was eligible but did not assemble context (`cold_start`, `disabled`, `inline_timeout`, `no_result`, `timeout`, `exception:…`).
- Added `is_available()` to `rtk.py` — distinguishes fail-open (package missing) from active RTK.
- Added `set_rtk_stats()` and `set_curator_skip_reason()` to `TraceBuilder`.
- Dashboard: RTK✓/RTK✗ badge on turn header; rtk savings in token line; curator skip reason label on passthrough user turns.

### Step 2 — Config experiments
- `AGENT_SOLO_MIN_INPUT_TOKENS=3000` in `.env` (lowered from default 8000 to compress nearly all multi-turn coding sessions from turn 2 onward).
- Added "Tuning experiments (2026-06-04)" section to `.agent/workflows/benchmarking.md` with four named variants (A–D): solo threshold, briefing staleness, synthetic tools off, background pass comparison.

### Step 3 — Proxy-driven recall
- Added `detect_recall_trigger()` to `proxy/recall.py` — fires on explicit recall language in the last user message (`user_phrase`) or when the same file appears in ≥2 tool results in recent messages (`repeated_file_read`).
- Added `inject_proxy_recall_into_body()` — prepends a `[PROXY-RECALL | trigger=…]` block to the system message before upstream dispatch.
- `chat.py`: proxy-forced recall block runs between RTK filter and synthetic tools injection; logs `proxy_recall_injections` metric.
- Added `recall_trigger` field to `TurnTrace` (`"proxy_forced:<type>"` | `"model_invoked"` | `""`).
- Extended `TraceBuilder.set_recall()` with `trigger` kwarg.
- Dashboard: recall line shows `[trigger]` annotation.

### Step 4 — Curator tightening
- `curator/loop.py`: repeated `get_file`/`get_file_lines`/`prefetch_file` calls for the same path now append a `PROXY-NOTE` to the result discouraging re-fetch; repeated `search_facts`/`search_facts_semantic` calls for the same query similarly noted. Added `_seen_queries` set per run.
- `curator/pipeline.py`: `_run_with_briefing` now populates `_last_attempt` on inline timeout, exception, and no_result — inline pass failures now reach the dashboard's `curator_skip_reason`.
- `curator/prompts.py`: `_format_previous_snapshot` now emits a `PROHIBITED` prefix with an explicit list of banned tool calls for already-fetched file paths. Delta guidance tightened to "Re-fetching costs an iteration and produces no benefit."

### Post-review remediation
- `set_recall()` now records `model_invoked` explicitly when recall happened without a proxy-forced trigger.
- `TurnTrace` now records `rtk_chars_after`, `proxy_recall_chars_added`, `outbound_chars_sent`, and `rtk_strategy_savings` so RTK filter savings can be compared against final outbound payload size after proxy recall injection.
- Dashboard turn cards now render RTK strategy-level savings and final outbound char counts, including proxy-recall additions.
- Curator tool-log records now preserve `proxy_note` warnings for repeated file reads and repeated fact searches, and the dashboard renders those notes directly.
- Added targeted regression coverage for recall-trigger labeling, RTK/outbound trace stats, strategy breakdown persistence, and curator proxy-note serialization.

## 2026-06-01 — Briefing enabler: pluggable curation mode registration

- Added `SessionBriefing.mode` field (`"two_pass"` | `"two_curator"`) — tags which curation mode produced a briefing.
- Added `register_curation_mode()` / `unregister_curation_mode()` to `curator/__init__.py` — lets the two-curator mode swap in its own prepper/assembler functions while the single-bot two-pass mode remains the default fallback.
- Refactored `run_background_pass()` to dispatch to a registered background pass function when present, falling back to `_run_background_pass_inner()` otherwise.
- Refactored `curate_context()` to dispatch to a registered inline pass function for briefing-assisted passes when present, falling back to `_run_with_briefing()` otherwise.
- Added `briefing_max_staleness` config key (default 2) — replaces the hardcoded `turn_number - 2` threshold in briefings staleness check.
- Added `CuratorResult.assembly_mode` field (default `"curator"`) — enables mode-specific results to self-identify their assembly path.
- 18 new tests: registration hooks (6), background pass dispatch (3), inline pass dispatch (3), briefing mode field (3), CuratorResult assembly_mode (3). All 54 tests pass.
- Full backward compatibility — single-bot two-pass behavior is unchanged when no mode is registered.

## 2026-05-30 — Two-pass curator: background pre-fetch + inline briefing

- Added two-pass curator architecture (disabled by default, `BACKGROUND_PASS_ENABLED=true`).
  - **Background pass:** after each upstream response, an async curator loop runs with up to `BACKGROUND_PASS_MAX_ITERATIONS` (default 12) tool calls and caches a `SessionBriefing` for the next turn.
  - **Inline pass:** on the next request, if a fresh briefing is available, the curator runs with only 2 iterations using the pre-fetched briefing (files, outlines, key facts) instead of re-discovering from scratch.
  - Fallback: if no briefing exists or the briefing is stale (`source_turn < turn_number - 2`), falls through to the standard full curator run.
- Added `raw_result` field to `CuratorToolCall` — stores the full tool result text so the briefing builder can use complete file contents instead of the 200-char preview. Excluded from `to_dict()` to keep traces bounded.
- Added `background_pass_latency_budget_ms` config (default 30000ms) — `asyncio.wait_for` timeout guard on `run_background_pass()`. On timeout, logs and returns silently without blocking the response.
- Added `background_pass_debounce_ms` config (default 2000ms) — minimum interval between background passes to avoid thrashing on rapid turns.
- Dashboard: `modeTag()` now handles `briefing` and `briefing_stale` assembly modes.
- 36 new tests (9 integration + 27 unit): background pass pipeline, inline briefing pipeline, raw-result fidelity, env-var config overrides.

## 2026-05-30 — License, CLA, and Benchmark Refresh

- Switched license from Apache 2.0 to PolyForm Noncommercial 1.0.0 (consistent with archolith-bench and archolith-rtk).
- Added CLA.md (Contributor License Agreement) and .github/pull_request_template.md with CLA checkbox.
- Updated CONTRIBUTING.md with CLA reference.
- Updated README benchmark section with archolith-bench headline numbers (58.6% proxy, 50% filter, 71.5% MCP waste).
- Added archolith trademark notice to README.
- Updated pyproject.toml: license, author email, repository URLs, benchmarks link.

## 0.3.0 — 2026-05-30 — SSE fix, circuit breaker, token budget

- **CRITICAL FIX:** `_wrap_response_as_sse()` now emits `tool_calls` deltas with proper `index` keys when converting non-streaming responses to SSE. Previously only emitted `role`, `content`, and `finish_reason` — any response with `finish_reason: "tool_calls"` but no tool call data caused OpenCode to error/retry infinitely, burning tokens until compaction killed the session.
- Added per-session circuit breaker for synthetic tool re-injection: after 3 consecutive failures, synthetic injection is disabled for 5 minutes; after 10 total failures, disabled for session lifetime.
- Added per-session token budget (`MAX_INPUT_TOKENS_PER_SESSION`, default 2M) with configurable action (`SESSION_TOKEN_BUDGET_ACTION`: "passthrough" or "reject").
- Added synthetic tool metrics to `/metrics`: `synthetic_tool_successes`, `synthetic_tool_failures`, `synthetic_circuit_opens`, `synthetic_circuit_hard_disables`, `synthetic_injections_skipped`, `synthetic_circuit_states`.
- Improved synthetic tool fallback message to redirect the agent to use file tools directly instead of leaving it confused.
- Added `SyntheticResult.fallback_used` flag to distinguish fallback-stripped responses from successful re-sends (enables circuit breaker feedback).
- Added 18 unit tests for SSE tool_calls conversion, circuit breaker, and token budget.
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
