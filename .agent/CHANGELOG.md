# Changelog — archolith-context

## 2026-06-20 — Live Stream WebSocket Route Coverage

- **Tests**: Added `/ws/stream` route integration coverage for event delivery and overflow close handling.

## 2026-06-20 — Session Retention And Consent Controls

- **`DELETE /admin/sessions/{session_id}`**: Added an admin deletion path that coordinates graph and trace-store deletion through the shared `archolith_compliance.retention` report contract.
- **`GET /admin/sessions/{session_id}/stored`**: Added an admin enumeration endpoint for graph presence, active fact count, cached file count, trace turns, background passes, metadata keys, and trace JSONL file status.
- **`SESSION_CONSENT_REQUIRED`**: Added an operator setting and `X-Session-Consent: opt-in` protocol. When required consent is missing, trace-store writes are skipped for that request context.
- **Docs/config**: Documented processing purpose, retention defaults, deletion endpoints, consent protocol, and the new `.env.example` settings.

## 2026-06-20 — Structured Log PII Redaction

- **`archolith_proxy/compliance.py` + `archolith_proxy/config/groups/compliance.py`**: Added a setting-driven log redaction wrapper around `archolith_compliance.redact`, defaulting to `truncated_32`.
- **`archolith_proxy/openai/extraction.py`, `extractor/client.py`**: Redacted structured-log `goal` and extraction parse-error `content` fields without changing stored facts, trace payloads, broadcasts, or extraction prompts.
- **Docs/config**: Documented `LOG_PII_REDACTION_LEVEL`; startup logs when operators select a non-default redaction level.

## 2026-06-20 — Benchmark Session Override Removal

- **`archolith_proxy/proxy/session.py`, `trace/router.py`, `openai/chat.py`**: Removed the process-global benchmark session-ID override API and state. Session identity now stays request-scoped via `X-Session-ID`/`x-session-affinity`, including passthrough trace recording.
- **`scripts/harness_benchmark.py`, `scripts/scripted_benchmark.py`**: Migrated benchmark setup away from `/trace/benchmark/session-id`; scripts now print or pass explicit `X-Session-ID` values for benchmark sessions.
- **`.agent/architecture.md`**: Removed the deleted benchmark session override endpoint from the current endpoint table.

## 2026-06-20 — Compliance Package Dependency

- **`pyproject.toml`**: Added the `compliance` optional dependency group for `archolith-compliance>=0.1.0` and included it in the `full` extra so upcoming retention, consent, and PII-redaction adoption can use the shared package.

## 2026-06-20 — Config Package Split

- **`archolith_proxy/config/`**: Converted the oversized `config.py` module into a package with domain field groups, a `Settings` model module, runtime override helpers, profile constants, and path constants.
- **Compatibility**: Preserved the existing `archolith_proxy.config` import surface, including private helpers used by current tests and admin config code; no settings or defaults changed.

## 2026-06-20 — Chat Module Split

- **`archolith_proxy/openai/chat.py`**: Split passthrough handlers into `chat_passthrough.py` and session overlay helpers into `chat_overlay.py`; `chat.py` re-imports the helper names for compatibility.

## 2026-06-20 — Outstanding security surface remediation

- **`archolith_proxy/routers/live_router.py`**: `/ws/stream` now follows the approved dedicated boundary: `ADMIN_TOKEN` when configured, otherwise loopback-only unless `WS_ALLOW_ANONYMOUS=true`.
- **`archolith_proxy/config.py` / `main.py`**: Default CLI bind host is `127.0.0.1` via `PROXY_HOST`; non-loopback binds without `ADMIN_TOKEN` warn at startup; CORS defaults to a loopback-origin regex unless `cors_allowed_origins` is explicitly set; `["*"]` is a warning-backed legacy opt-in.
- **`archolith_proxy/config.py` / `main.py`**: Non-loopback plaintext HTTP is rejected for `upstream_base_url`, `extractor_base_url`, `embedding_base_url`, `curator_base_url`, and `prepper_base_url` unless `ALLOW_INSECURE_UPSTREAM_URL=true`; explicit opt-in logs the insecure setting names.
- **`archolith_proxy/config.py` / `main.py`**: Startup refuses `curator_enabled=true` with unrestricted prefetch filesystem access unless `I_ACCEPT_UNRESTRICTED_FS_RISK=true` is set.
- **`archolith_proxy/config.py` / `routers/admin_router.py`**: `curator_enabled`, `filter_enabled`, `native_read_intercept_enabled`, `synthetic_tools_enabled`, and `drop_middle_on_assembly` are blocked from unauthenticated per-session overrides; `synthetic_tools_enabled` is no longer admin-runtime tunable and returns 422 if patched.
- **`archolith_proxy/openai/chat.py`, `curator/briefing.py`, `extractor/prompts.py`, `session_goal.py`**: Cold-start goal creation from the first user message is removed. Extraction-driven goal updates are sanitized on storage, and stored goals are quoted as data in curator/extractor prompts.
- **Tests/docs**: Added targeted regressions for WebSocket auth, config denylist/runtime tunables, URL validation, CORS middleware behavior, prefetch focus-path allowlists, and goal prompt framing. Updated `.env.example`, README, architecture, and data-model docs for the operator defaults.
- **Sequencing note**: This is a forward correction to the current security-hardening changes. The approved Wave A refactors (`config.py` package split and `chat.py` split) are still outstanding.

## 2026-06-11 — Helper-cost telemetry review closure

- **`archolith_proxy/trace/builder.py`**: `set_helper_usage()` now preserves prior helper-stage fields across multiple calls so curator usage is not zeroed by later extractor/embedding updates.
- **`archolith_proxy/extractor/client.py`**: Per-tool extraction now returns combined helper usage, including per-tool LLM calls plus turn-level prompt/completion usage.
- **`archolith_proxy/extractor/extractors/{bash,default,web_fetch}.py`**: LLM-backed extractors now capture upstream prompt/completion usage when available.
- **Tests**: Added regressions for helper-usage preservation and per-tool usage propagation.

## 2026-06-10 — Mechanical Mode — ARCHOLITH_PROFILE flag bundles

- **`archolith_proxy/config.py`**: Added `ARCHOLITH_PROFILE` setting (passthrough | mechanical | curated | full) with `PROFILES` dict defining each bundle. `_apply_profile()` applies flags only when NOT explicitly set by env (env-wins precedence). Added to SESSION_CONFIG_DENYLIST. Profile exposed in `/admin/config` via config snapshots.
- **`archolith_proxy/main.py`**: Logs active profile + bundle on startup. Profile-driven `filter_enabled` degrades gracefully when `archolith_filter` is missing (full passthrough degradation clears all profile-enabled flags). Explicit `FILTER_ENABLED=true` still fails fast.
- **`.env.example`**: Added `ARCHOLITH_PROFILE=mechanical` with docstring describing all four profiles.
- **`README.md`, `docker-compose.yml`, `.agent/architecture.md`**: Updated for mechanical default profile.
- **Tests**: 16 tests covering profile definitions, env-wins precedence, snapshot inclusion, and startup degradation per-profile.

## 2026-06-10 — Helper-LLM Cost Telemetry (extractor/curator/embedding token usage)

- **`archolith_proxy/models/dtos.py`**: Added 6 helper-LLM usage fields to `TurnTrace` (extractor_prompt/completion_tokens, extractor_llm_calls, curator_prompt/completion_tokens, embedding_tokens), 2 fields to `BackgroundPassTrace` (prompt/completion_tokens_used), and `usage` field to `ExtractionResult`.
- **`archolith_proxy/trace/builder.py`**: Added `set_helper_usage()` to accept extractor/curator/embedding token counts.
- **`archolith_proxy/extractor/client.py`**: `extract_facts()` and `extract_facts_per_tool()` now capture upstream LLM usage from the API response into `ExtractionResult.usage`.
- **`archolith_proxy/extractor/embeddings.py`**: `compute_embeddings_batch()` now returns `(embeddings, total_tokens)` tuple capturing usage from the embeddings API.
- **`archolith_proxy/curator/result.py`**: Added `prompt_tokens_used` and `completion_tokens_used` to `CuratorResult`.
- **`archolith_proxy/curator/loop.py`**: `_run_curator_native()` accumulates `response.usage` per iteration and passes through to `CuratorResult`.
- **`archolith_proxy/curator/pipeline.py`**: Both `curate_context()` and `_run_with_briefing()` thread curator token usage through `AssembledContext`.
- **`archolith_proxy/openai/extraction.py`**: Updated `_compute_fact_embeddings` return type to include token usage; wires extractor + embedding usage into trace builder and `/metrics`.
- **`archolith_proxy/metrics.py`**: Added 5 cumulative counters: `extractor_prompt_tokens_total`, `extractor_completion_tokens_total`, `curator_prompt_tokens_total`, `curator_completion_tokens_total`, `embedding_tokens_total`.
- **Tests**: 12 new tests covering DTO serialization, trace builder, CuratorResult/ExtractionResult/BackgroundPassTrace fields, and embedding usage capture.

## 2026-06-11 — Documentation Reconciliation Review Fixes

- **ARCHITECTURE.md §3**: Removed the contradictory active skip-reason bullet for the unenforced `ASSEMBLY_MIN_SAVINGS_RATIO`; the section now lists only enforced skip reasons and keeps savings-ratio/latency-budget as recorded-but-not-enforced knobs.
- **.agent/architecture.md**: Corrected the remaining two-curator prepper `CURATOR_MAX_ITERATIONS` default from 4 to 6.

## 2026-06-10 — Extraction Batching at User-Turn Boundaries

- **`archolith_proxy/openai/extraction.py`**: Extracted file-cache capture into `_run_file_cache_capture()` (runs on every request). Added `_is_turn_boundary()` helper. Added turn-boundary guard: when `extraction_mode="turn_boundary"`, LLM extraction runs only on user-turn boundaries or `finish_reason="stop"` — skipping the ~85% of agent-solo continuations without information loss (the client resends full history). `_run_file_cache_capture` always runs regardless of mode.
- **`archolith_proxy/config.py`**: Added `extraction_mode: str = "turn_boundary"` — accepts `"turn_boundary"` (default) or `"every_turn"` (legacy behavior).
- **`.agent/architecture.md`**: Updated data-flow step 8 to document the turn-boundary default and fallback mode.
- **Test**: 16 new tests — 11 parametrized `_is_turn_boundary` truth-table checks, 5 integration tests covering skip-on-agent-solo, run-on-user-turn, run-on-finish-stop, file-cache-runs-every-turn, and every_turn mode compatibility.
- **Remediation**: `_collect_tool_call_records()` now collects every assistant/tool-call batch in the completed turn, including the previous completed agent turn when extraction runs on the next user request. Added regression tests for multi-batch per-tool extraction and real file-cache upsert on skipped continuation turns.

## 2026-06-10 — Documentation Reconciliation (10 code/docs mismatches fixed)

- **ARCHITECTURE.md §3**: Corrected assembly gate description — only `ASSEMBLY_MIN_INPUT_TOKENS` is enforced; savings-ratio and latency-budget knobs recorded as not-yet-enforced. Section §4 clarified that the heuristic fact-ranking assembler serves the `__archolith_recall` tool, not the main chat assembly path (which uses curator or passthrough).
- **.agent/architecture.md**: `CURATOR_MAX_ITERATIONS` default corrected to 6 (was 4); Graphiti removed from tech stack with cross-reference to dead-code-removal plan; synthetic tools section given deprecation banner; assembler section clarified as recall-tool-only; smart-tail description updated with known-limitation note.
- **.agent/workflows/benchmarking.md**: All 7 tunable defaults corrected (coherence_tail_size 3→10, cold_start_turns 1→3, cold_start_token_threshold 200→20000, embedding_enabled/query_rewrite_enabled/compaction_enabled/session_recall_tool_enabled true→false). Maintenance note added.
- **README.md**: Re-read claim qualified (file cache + curator tools path; native-read-intercept deprecated/disabled).
- **.env.example**: EXTRACTOR_API_KEY and EMBEDDING_API_KEY comments corrected (keys must be set explicitly; no fallback).
- **CLAUDE.md**: Title updated from `cth.context-engine` to `archolith-context`.
- **ARCHITECTURE.md component table**: smart_tail integrity-limitation note added.
- All fixes verified against `config.py` and `chat.py` on `main`.

## 2026-06-09 — Plugin System (ProxyPlugin contract + built-in plugins + unified distribution)

- **`archolith_proxy/plugins/`** (new): `ProxyPlugin` `@runtime_checkable` Protocol with six lifecycle members (`plugin_id`, `plugin_version`, `activate`, `deactivate`, `healthcheck`, `contribute_metrics`). `PluginRegistry` singleton manages lifecycle with fail-safe contract — no plugin misbehavior can prevent proxy startup. `PLUGINS_ENABLED` / `PLUGINS_DISABLED` env var gating; `MIN_PLUGIN_VERSIONS` enforces minimum compatible versions with clear error logging.
- **Built-in plugins**: `FilterPlugin` (wraps `filter_adapter.py` sentinels; exposes `FilterTelemetryStore` stats), `MemoryPlugin` (reads `MemoryEngineRegistry`; reports engine count + promotion counters), `AuditPlugin` (availability probe for `archolith_mcp_audit`; optional `LiveAccumulator` attachment). All three auto-registered at proxy startup.
- **`archolith_proxy/routers/plugins.py`**: `GET /plugins` (list with summary counts), `GET /plugins/{id}` (detail + live health + metrics). Registered with admin auth.
- **`GET /metrics`**: extended with `plugins` key — aggregated plugin metrics grouped by plugin ID.
- **Dashboard**: Plugins card shown when any plugin is registered.
- **`pyproject.toml`**: bumped to `0.5.0`; added `[filter]`, `[audit]`, `[full]` optional extras (`pip install archolith-proxy[full]`).
- **README**: Install section with one-liner extras commands; `archolith-proxy` CLI replaces `python -m`.
- **57 tests** covering protocol compliance, fail-safe lifecycle, config gating, version compat, health, metrics aggregation, router endpoints, and all three built-in plugin wrappers.

## 2026-06-08 — Structural Token Accounting (TODO #8, trace + gating)

- **`archolith_proxy/token_accounting/`** (new): structural token estimator salvaged from the `feat/evaluation-and-rollout` branch. Counts tool schemas, `tool_calls`, tool-result payloads, and message framing that the crude `len(json.dumps)//4` estimate missed. `build_telemetry` produces content / structural / client-reported estimates + a gate decision; `extract_client_hint` reads `X-Context-Token-Hint`. Uses tiktoken (cl100k_base) with a `len/3.6` fallback. 34 ported unit tests.
- **`archolith_proxy/openai/chat.py`**: the assembly gate now keys on `gate_input_tokens` (structural) instead of the crude messages-only estimate, which was blind to the `tools` array (e.g. 10 vs ~17,900 tokens on a 20-tool request). `build_telemetry` runs via `asyncio.to_thread` — tiktoken releases the GIL (verified ~2.7x parallel), so encoding does not block the event loop. Legacy `input_tokens` retained for the session token budget.
- **`archolith_proxy/trace/builder.py` + `models/dtos.py`**: `TurnTrace` gains token-accounting fields (`token_content_est`, `token_structural_est`, `token_client_reported`, `token_gate_input`, `token_gate_source`, `token_estimator_version`) and `prompt_tokens_actual` (actual upstream input tokens) for estimate-vs-actual reconciliation. `set_response` captures `prompt_tokens`.
- **Calibration**: `assembly_min_input_tokens` 50K -> 55K and `assembly_min_savings_ratio` 0.20 -> 0.25, re-tuned for structural tokens (which run larger than content-only and still undercount upstream by ~10.7%). `eval/calibration_runner.py` + `eval/calibration_corpus.py` (dev tooling, not imported by the proxy) compare old vs new estimator against simulated client/upstream values and confirm the 55K/0.25 recommendation.
- **Metrics**: added `total_input_tokens_client_reported` and `gate_decisions_<source>` counters (which estimate source the gate used). Registered `total_input_tokens_structural` (a live smoke caught it unregistered).
- **Tests**: 837 passed. Added a trace test asserting the telemetry surfaces through `build()` (guards the DTO from silently dropping the new fields).

## 2026-06-08 — Per-Session Config Overrides

- **`archolith_proxy/config.py`**: Added a `contextvars` per-session settings overlay. `get_settings()` returns the session overlay when active, else the global singleton (default — behavior-identical across all ~54 call sites). `build_effective_settings()` layers session overrides over the global base (precedence session > `config_overrides.json` > env > default); `SESSION_CONFIG_DENYLIST` blocks per-session override of secrets/infra. Added `set_session_settings` / `reset_session_settings`.
- **`archolith_proxy/graph/` (ladybug_backend, ladybug_sessions, session, neo4j_backend, protocol)**: Added a `config_overrides` column on the Session node with an idempotent ALTER migration for pre-existing DBs, and symmetric `set/get_session_config_overrides` CRUD. LadybugDB 0.16.1 mangles a STRING parameter beginning with `{` (stores a STRUCT repr), so the ladybug CRUD base64-encodes on write and decodes via the typed getter; neo4j stores verbatim.
- **`archolith_proxy/openai/chat.py`**: An `X-Session-Config` request header merges into the session's persisted overrides, persists, and activates the overlay for the request; a request-scoped dependency resets it after the response. Denied/unknown fields are rejected loudly and never persisted. Async follow-up work (extraction, curator background pass) inherits the overlay via context copying.
- **`archolith_proxy/routers/admin_router.py`**: `PATCH /admin/config?persist=false` applies an override in-memory only (no `config_overrides.json` write) so benchmark runs don't mutate global config.
- **Tests**: Added `tests/test_per_session_config.py` (overlay precedence/denylist/coercion, contextvar propagation, request-helper merge/persist against fake + real LadybugDB) and graph round-trip/migration coverage. Full suite: 802 passed.

## 2026-06-02 — Quality Remediation Closeout Follow-Through

- **`archolith_proxy/main.py`**: Fixed post-expiry cache pruning to remove stale session state instead of clearing active agent-solo entries. Cleanup now prunes both agent-solo caches and curator briefing/snapshot state after graph expiry cycles.
- **`archolith_proxy/proxy/agent_solo.py`**: Added `prune_session_state()` to drop inactive dedupe and curator-prefix cache state in one place.
- **`archolith_proxy/curator/state.py`**: Added `prune_session_state()` so expired sessions clear cached briefings, snapshots, and any in-flight background pass task.
- **`archolith_proxy/curator/tools.py`**: Promoted `_build_outline` to a module-level import; `prefetch_file()` no longer does a function-body lazy import.
- **`archolith_proxy/curator/__init__.py`**: Trimmed the public surface module below the plan’s aspirational size target while keeping `configure_curation_mode()` behavior unchanged.
- **`.agent/architecture.md`**: Updated the project architecture doc to reflect the extracted OpenAI modules, dedicated router files, shared text utilities, trace consistency check, and `config-delta` operator surface.
- **Tests**: Added regression coverage for stale-session cache pruning, bulk Ladybug writes, runtime config override persistence, and trace-store consistency checks.

## 2026-06-01 — Two-Curator Architecture (Prepper + Assembler)

- **`archolith_proxy/config.py`**: Added `curation_mode` (two_pass | two_curator), `prepper_*` model configs (model/base_url/api_key/max_iterations/debounce_ms/latency_budget_ms), and `assembler_*` model configs (model/base_url/api_key/max_iterations/latency_budget_ms). Added these to `_SNAPSHOT_EXCLUDE` for secrets.
- **`archolith_proxy/curator/prepper.py`**: New background prepper module with `PREPPER_SYSTEM_PROMPT` (optimized for speculative context preparation) and `run_prepper()`. Uses `PREPPER_TOOLS` and independent model config. Produces `SessionBriefing` with `mode="two_curator"`.
- **`archolith_proxy/curator/assembler.py`**: New inline assembler module with `ASSEMBLER_SYSTEM_PROMPT` (optimized for fast briefing formatting) and `run_assembler()`. Uses `ASSEMBLER_TOOLS` (minimal: select_relevant_turns + get_file_lines) and tight iteration/ latency budget.
- **`archolith_proxy/curator/schemas.py`**: Added `PREPPER_TOOLS` (all curator tools + `score_file_relevance`), `ASSEMBLER_TOOLS` (select_relevant_turns + get_file_lines), and `SCORE_FILE_RELEVANCE_SCHEMA`.
- **`archolith_proxy/curator/tools.py`**: Added `score_file_relevance()` — heuristic file relevance scorer for the prepper. Scores files by keyword match in path/outline and recency. Registered in `TOOL_HANDLERS`.
- **`archolith_proxy/curator/loop.py`**: Added optional `tool_set` parameter to `_run_curator_native()` and `_run_curator_nous()` so the assembler can use a filtered tool set. Defaults to `ALL_CURATOR_TOOLS` for backward compatibility.
- **`archolith_proxy/curator/__init__.py`**: Added `configure_curation_mode()` — reads `settings.curation_mode`, registers prepper/assembler when `"two_curator"`, unregisters otherwise. Idempotent.
- **`archolith_proxy/main.py`**: Calls `configure_curation_mode()` in the lifespan startup. Added `curation_mode`, `prepper_*`, `assembler_*` to `TUNABLE_FIELDS` for runtime config.
- **`archolith_proxy/static/dashboard.html`**: Added Curation Mode card to overview page showing mode, curator/prepper/assembler model names, iterations, and background pass state.
- **`tests/test_curator/test_two_curator.py`**: 15 new tests covering registration hooks, configure_curation_mode dispatch, prepper/assembler tool sets, score_file_relevance handler, prepper no-API-key/timeout paths, and assembler no-API-key path.

## 2026-05-31 — Agent-Solo Compression, Curator Prefix Cache, Dashboard Fixes

- **`archolith_proxy/openai/chat.py`**: Fixed agent-solo savings being zeroed by catch-all `set_assembly` overwrite — replaced early `set_assembly` with direct variable updates. Fixed broadcast zeroing savings for non-assembled turns. Added `cache_curator_rewrite()` call after successful curator rewrite. Added Java/Kotlin/C# regex fallback patterns to `_build_outline()` for file outline generation.
- **`archolith_proxy/proxy/agent_solo.py`**: Rewrote module with curator prefix cache — `_CuratorCache` stores `{original_count, fingerprint, rewritten}` after curator rewrites; `_apply_curator_prefix()` splices cached rewrite into agent-solo turns via O(1) count + md5 fingerprint check. Added `chars_saved_curator_cache` and `chars_saved_compact` to stats dict. `compress_agent_solo()` runs two-phase pipeline: curator prefix cache → RTK Layer 3 strategies.
- **`archolith_proxy/static/dashboard.html`**: Split "Turns" (user turns) and "API Calls" columns in session list. Added null-safe sort with explicit null-last handling. Incremental merge now only triggers DOM rebuild on meaningful field changes. Turn cards show raw input as "ctx" with "→ N sent" when savings exist, plus color-coded savings delta.
- **`archolith_proxy/config.py`**: Lowered `agent_solo_min_input_tokens` from 30K to 8K — sessions at 20K were below threshold and never compressed.
- **`archolith_proxy/main.py`**: Expanded `TUNABLE_FIELDS` for `PATCH /admin/config` to include `agent_solo_*`, `curator_*`, `synthetic_tools_enabled`, `drop_middle_on_assembly`.
- **`.agent/architecture.md`**: Added agent-solo component section, updated data flow to show agent-solo path (2a) with curator prefix cache and RTK Layer 3, updated assembly_modes list, added Layer 3 to RTK layers table, added `AGENT_SOLO_*` env vars to config reference.

## 2026-05-31 — Remove Faulty Restart Script

- **`scripts/restart_proxy.py`**: Removed the faulty alternate restart helper. In practice it could report a successful restart while failing to leave a durable background proxy process running in this environment.
- **`scripts/README.md`**: Clarified that `scripts/proxy_restart.py` is the canonical restart path and that alternate restart helpers should not be added unless they preserve the same durable launch behavior and logging.

## 2026-05-31 — Deep-Dive Doc Refresh for Current Proxy State

- **`.agent/README.md`**: Added a naming map for `archolith-context` vs `archolith_proxy` vs `archolith-proxy`, documented `design.md`, `mcp-extractor-ideas.md`, `prompts/`, and warned that `.agent/worktrees/` snapshots are not the live source of truth for current docs.
- **`.agent/architecture.md`**: Brought the architecture narrative in line with the live repo by documenting the current naming reality, the `GraphBackend` abstraction, the code-default `neo4j` vs bootstrap-friendly `ladybug` split, per-tool extraction, native read interception, and the current operator/benchmark endpoints (`/live`, `/ready`, `/admin/config`, `/admin/shutdown`, `/trace/benchmark/session-id`).
- **`.agent/data_models.md`**: Replaced the older Neo4j-first `cth.context-engine` model reference with a current model map covering the shared graph node shapes, file-cache records, trace DTOs, backend contract, and promotion/memory-engine payloads used by `archolith_proxy`.

## 2026-05-26 — Per-Tool Extraction Post-Review Fixes

- **`archolith_proxy/extractor/base.py`**: Added `may_use_llm: bool = False` class attribute to `ToolExtractor` ABC. Extractors that can make API calls declare `may_use_llm = True`; no-LLM extractors inherit the `False` default.
- **`archolith_proxy/extractor/extractors/bash.py`**, **`web_fetch.py`**, **`default.py`**: Set `may_use_llm = True` — these three extractors may make LLM API calls.
- **`archolith_proxy/extractor/client.py`**: `_extract_with_semaphore()` now gates only on `extractor.may_use_llm`. No-LLM extractors (Grep, Glob, LS, Find, Read, WriteEdit, WebSearch, MemoryRecall) bypass the semaphore and run fully concurrently. Replaced fragile `"turn_result" in dir()` logger guard with explicit `_turn_level_facts_count` counter variable.
- **`tests/test_extractor/test_per_tool_extraction.py`**: Added `TestBashExtractor.test_pipe_with_recognizable_primary` and `TestExtractFactsPerTool.test_semaphore_only_applied_to_llm_backed_extractors`. 588 tests passing (commit `30ccc98`).

## 2026-05-26 — Per-Tool Extraction Dispatch System

- **`archolith_proxy/extractor/base.py`**: New — `ToolCallRecord` dataclass (tool_call_id, tool_name, args, result), `PartialExtractionResult` dataclass (source_tool, facts, files_touched, used_llm), `ToolExtractor` ABC with `tool_names` tuple and abstract `extract()`.
- **`archolith_proxy/extractor/registry.py`**: New — `ToolExtractorRegistry` with exact-match + longest-prefix-match routing (prevents ambiguity for overlapping prefix sentinels). `build_default()` factory, `get_registry()` process-level singleton.
- **`archolith_proxy/extractor/extractors/`**: 10 new extractors — `GrepExtractor` (path:line:match parsing, no LLM), `GlobExtractor` (file list, no LLM), `LsExtractor` (directory entries, no LLM), `FindExtractor` (path count, no LLM), `WebSearchExtractor` (JSON-first + regex fallback, no LLM), `WebFetchExtractor` (LLM with `WEB_FETCH_SYSTEM_PROMPT`), `BashExtractor` (regex pre-pass — pytest/jest/cargo/go/git patterns + universal error; LLM fallback; ANSI stripping; builtin fallthrough), `MemoryRecallExtractor` (JSON parse, score filter <0.5, cap 20, prefix sentinel `mcp__memory__recall`), `DefaultExtractor` (LLM catch-all using existing `SYSTEM_PROMPT`). Plus existing `ReadExtractor` and `WriteEditExtractor` untouched.
- **`archolith_proxy/extractor/prompts.py`**: Added `BASH_SYSTEM_PROMPT` + `build_bash_extraction_prompt()`, `WEB_FETCH_SYSTEM_PROMPT` + `build_web_fetch_extraction_prompt()`, `TURN_LEVEL_SYSTEM_PROMPT` + `build_turn_level_extraction_prompt()`. Turn-level prompt explicitly forbids `tool_result`/`file_state` fact types and prevents LLM from inferring tool output.
- **`archolith_proxy/extractor/client.py`**: New `extract_facts_per_tool()` orchestrator — async fan-out via `asyncio.gather(return_exceptions=True)`, semaphore-capped LLM concurrency (`extractor_llm_concurrency`, default 3), explicit `isinstance(r, Exception)` merge guard, turn-level LLM call for decisions/checkpoint/issues/verifications, MD5 dedup between per-tool and turn-level facts. Old `extract_facts()` kept unchanged.
- **`archolith_proxy/openai/chat.py`**: Added `_build_call_map()` shared utility; refactored `_extract_file_reads()` to use it; new `_collect_tool_call_records()` (applies RTK Layer 1 filter); feature-flag gate routes to `extract_facts_per_tool()` when `per_tool_extraction_enabled=True`.
- **`archolith_proxy/config.py`**: Added `per_tool_extraction_enabled: bool = False` and `extractor_llm_concurrency: int = 3`.
- **`tests/test_extractor/test_per_tool_extraction.py`**: 52 new tests across 16 test classes covering all extractors, registry routing, `_build_call_map`, `_collect_tool_call_records`, orchestrator exception handling, turn-level prompt validation, and integration gate. 586 tests passing (commit `30d7dde`).

## 2026-05-26 — Archolith Ecosystem Docs + Per-Tool Extraction Plan

- **`.agent/architecture.md`**: Added *Archolith Ecosystem* section — archolith-rtk / archolith-memory / archolith-context module table with roles and dependency models. Design constraints: standalone `pip install`, fail-open peer imports, MCP as thin wrapper. Captures planned archolith-memory integration shape (read=proxy recall, write=promotion pipeline, explicit=MCP).
- **`.agent/mcp-extractor-ideas.md`**: New reference doc for user-deployment-specific MCP extractors (cth.mcp.delegate, mcp__vps, mcp__sage-wiki) and the `register_from_config()` extension point design. Agnostic extractors remain in the main plan.
- **`.agent/plans/archolith-per-tool-extraction-plan.md`** (workspace root): Full OOP per-tool extraction design — `ToolExtractor` ABC, `ToolCallRecord`, `PartialExtractionResult`, `ToolExtractorRegistry` with prefix-match routing. 10 concrete extractors (Read, Bash, Grep, Glob, LS, Find, WebSearch, WebFetch, WriteEdit, MemoryRecall, Default), async fan-out orchestrator, `BASH_SYSTEM_PROMPT` + `WEB_FETCH_SYSTEM_PROMPT`, config flag `per_tool_extraction_enabled=False`. Status: DRAFT — not yet implemented.

## 2026-05-26 — Semantic Search Over Facts (13th Curator Tool)

- **`archolith_proxy/curator/tools.py`**: Added `search_facts_semantic(session_id, query, limit=10)` — cosine similarity over stored fact embeddings. Inline `_cosine()` helper. Creates `httpx.AsyncClient` on demand. Three-tier fallback: semantic → substring (no key or embed fails) → empty. Threshold 0.05 filters near-orthogonal facts. Added to `TOOL_HANDLERS`.
- **`archolith_proxy/curator/schemas.py`**: Added schema for `search_facts_semantic` between `search_facts` and `get_session_goal`. Parameters: `query` (required), `limit` (optional, default 10).
- **`archolith_proxy/curator/prompts.py`**: Added tool to available list. New rule 5: prefer `search_facts_semantic` when terminology may differ from stored facts; do not call both for the same query.
- **`tests/test_curator_tools.py`**: 15 new tests — `TestSearchFacts` (5), `TestSearchFactsSemantic` (7), `TestCosineLogic` (3). Injects openai stub to prevent local shadow import conflict.
- **`.agent/ROADMAP.md`**: Promoted to Done tier (commit `c149306`). 534 tests passing.

## 2026-05-26 — File Structure Index on Cache Ingest (12th Curator Tool)

- **`archolith_proxy/graph/ladybug_backend.py`**: Added `FileOutline` node table (`outline_id`, `session_id`, `path`, `outline`, `last_updated_turn`). Added `upsert_file_outline()` and `get_file_outline()` methods.
- **`archolith_proxy/graph/protocol.py`**: Added `upsert_file_outline` and `get_file_outline` to the `GraphBackend` protocol.
- **File cache ingest (`chat.py`)**: `_upsert_file_cache()` now calls `_build_outline()` after writing `FileContent` — AST (ast.parse) for Python, regex fallback for all other file types. Outline captures function/class definitions with line numbers.
- **`archolith_proxy/curator/tools.py`**: Added `get_file_outline(session_id, path)` as the 12th curator tool. Returns the stored symbol index for large files so the curator can call `get_file_lines` for targeted ranges rather than fetching the full file.
- **`archolith_proxy/curator/schemas.py`**: Added schema for `get_file_outline`.
- **`archolith_proxy/curator/prompts.py`**: Rule 3 updated — for files over 100 lines, call `get_file_outline` first, then `get_file_lines` for the relevant range. Skip outline only for data/config files with no symbols.
- **`.agent/ROADMAP.md`**: Promoted to Done tier (commit `94e182d`). 519 tests passing.

## 2026-05-26 — RTK Deep Integration + Curator One-Liners

### RTK integration (archolith_proxy/rtk.py, chat.py, rewrite.py)
- **rtk.py rewritten** as a first-class RTK adapter.  Added three new fail-open wrappers
  backed by `archolith-rtk` (our canonical token reduction library, optional peer):
  - `filter_single_tool_result(content, tool_name)` — Layer 1 per-string filter
  - `shrink_tool_call_args(messages, max_tokens, enabled)` — Layer 2 collapse of Write/Edit args
  - `shrink_tail_tool_results(messages, max_tokens_per_result)` — Layer 2 token cap per tail message
  - Lazy-load split into independent sentinels: `_load_filter_output()` and `_load_shrink_functions()`
  - `filter_request_body()` now chains `filter_tool_messages()` → `shrink_tool_call_args()`
- **chat.py** `_collect_recent_tool_results()`: applies `filter_single_tool_result` per tool
  result before packing into the 4000-char extraction budget — extractor LLM sees clean signal
- **rewrite.py** tail append: applies `shrink_tail_tool_results` on validated coherence tail —
  large file reads kept for structural integrity no longer bloat the context window
- **architecture.md** updated with full RTK section: layer reference, adapter API table,
  integration point diagram, project relationship, install instructions
- **Data Flow section** updated to show RTK passes inline at each pipeline step

### Curator one-liners (curator/__init__.py, curator/prompts.py)
- Pre-fetch checkpoint from graph backend and inject directly into curator user prompt —
  saves one full LLM tool-call iteration (~1-2s) per curator run
- `build_curator_user_prompt()` accepts `checkpoint: dict | None`; formats as inline block
- System prompt rule 1 updated: skip `get_checkpoint` since checkpoint is pre-loaded

### Assembler prompt cache fix (assembler/context.py)
- Removed per-turn counter from stable `=== SESSION OVERVIEW ===` section — was busting
  prompt cache every turn.  Turn marker moved to `[Turn: N]` in the facts footer (which
  changes every turn anyway, so no cache benefit lost)

### Write tool cache (chat.py)
- `_extract_file_writes()`: scans the most recent assistant message for Write/create_file
  tool_calls and extracts file content directly from the JSON arguments — content reaches
  the file cache without requiring the agent to do a subsequent Read

## 2026-05-22 — Benchmark Suite, Experiment Framework, RTK Plan

- **Benchmark suite** (`scripts/benchmark.py`): 5 scenarios (code_review, debugging, long_agent, ruler_recall, taskflow) with fact probes for recall measurement. Features: 429 retry with exponential backoff, checkpoint/resume, full response saving, markdown transcript generation, `--api-key` and `--resume` flags.
- **Admin config API** (`archolith_proxy/main.py`): `GET/PATCH/POST /admin/config` for runtime-tunable settings (budget, tail size, thresholds) without proxy restarts. Whitelist-validated with type coercion.
- **Experiment framework** (`scripts/benchmark.py` + `scripts/compare_experiments.py`): Named experiments snapshot proxy config alongside results for reproducible A/B tuning. `--experiment` and `--config` flags. Comparison script shows config diffs + per-scenario savings/recall/tokens side-by-side.
- **Benchmarking workflow** (`.agent/workflows/benchmarking.md`): Full proxy pipeline trace, 6 known recall loss points with file:line refs, experiment workflow, tunable settings reference, result interpretation guide.
- **RTK extraction plan** (`.agent/plans/archolith-rtk-extraction-plan.md`): Plan to extract the 3-layer token reduction system from reasonix TypeScript fork (output filters, shrink, context manager) and reimplement as `archolith-rtk` Python library.
- **Multi-model benchmarks in progress**: Matrix runs (5 scenarios × 4 budgets) on z.ai GLM-5.1 (port 9800) and DeepSeek V3 (port 9801). Early results: z.ai preserves recall better (128%) at moderate savings; DeepSeek compresses more aggressively but loses recall at 4K budget.
- **Recall analysis**: Identified 6 loss points — extraction truncation (8K char cap), numeric value extraction gaps, hardcoded 200-token overhead, coherence tail too small (3), linear recency bias, and DeepSeek output collapse at tight budgets.

## 2026-05-22 — Caller Compatibility Test Plan

- **Compatibility plan added**: Created `.agent/plans/archolith-caller-compat-plan.md` to define how `archolith-proxy` should be tested against major callers before making public compatibility claims
- **Caller matrix scoped**: Split callers into reference OpenAI SDK clients, OpenAI-compatible coding harnesses, and first-party clients (`Claude Code`, `Codex`) with per-caller pass/setup/adapter/policy/client-blocked statuses
- **Policy guardrails captured**: Documented the rule that API/commercial auth is the only safe compatibility lane for first-party provider clients unless broader approval is obtained
- **Budgeted execution order**: Defined zero-token pre-flight gates, shared smoke scenarios, and a strict caller order so compatibility can be proven without burning unnecessary model spend

## 2026-05-22 — Root Open-Source Documentation Set

- **Root docs created**: Added repository-root `README.md`, `ARCHITECTURE.md`, `CONTRIBUTING.md`, and `LICENSE` to support the open-source release path for `archolith-proxy`
- **README grounded in repo state**: Documented the proxy-first product positioning, OpenAI-compatible quick start, session model, recall tool, graph backends, and the committed `2026-05-21` benchmark audit rather than stale placeholder numbers
- **Architecture captured**: Added a repository-root architecture walkthrough covering request lifecycle, context assembly, smart tail rewriting, hidden recall interception, graph backend choices, observability surfaces, and current config caveats
- **Contribution path documented**: Added local setup, `uv` workflow, test/lint commands, and PR expectations aligned with the current Python/FastAPI stack
- **License surfaced**: Added Apache 2.0 license text at the repo root with `2026 Charles Harvey` copyright

## 2026-05-21 — Security, Resilience, Concurrency, and Memory Alignment Audit Templates

- **Audit Templates Added**: Created four new template files inside `.agent/` directory to structure ongoing system audits:
  - `SECURITY-PRIVACY-TEMPLATE.md`: Focuses on credentials leakage, authorization, database isolation, and upstream privacy.
  - `RESILIENCE-CHAOS-TEMPLATE.md`: Focuses on downstream outage simulation, timeouts, latency handling, and WAL crash recovery.
  - `CONCURRENCY-LOAD-TEMPLATE.md`: Focuses on memory profiling, cache evictions, turn-locking, and connection pool leaks.
  - `MEMORY-ALIGNMENT-TEMPLATE.md`: Focuses on fact promotion generalizability, adapter CRUD compatibility, and context drift.
- **Repository Hygiene & Tracking**: Tracked previously untracked templates (`BENCHMARK-AUDIT-TEMPLATE.md`, `CONTEXT-QUALITY-TEMPLATE.md`, `PRODUCT-READINESS-TEMPLATE.md`), baseline audits (`2026-05-21-gpt4omini-16turn-baseline.md`, `2026-05-21-context-quality-gpt4omini-baseline.md`), benchmark orchestration scripts (`scripts/benchmark_parallel.py`), and python package lock (`uv.lock`). Updated `.gitignore` to prevent tracking of local LadybugDB binaries/logs (`data/`, `*.lbug*`) and transient benchmark outputs (`scripts/*.json`, `audit_results.json`).


## 2026-05-20 — Graph Backend Adapter & LadybugDB (Phases 0-5)


- **Phase 0A — Cypher consolidation**: All inline Cypher moved from `trace/router.py` (5 blocks) and `assembler/context.py` (2 blocks) into `src/graph/`. Created `decisions.py` with `store_decision` / `get_decisions`. Added `get_facts_filtered`, `get_supersession_chain`, `get_invalidated_facts` to `facts.py`. Moved `list_active_sessions` / `get_session_stats` from `cleanup.py` to `session.py`.
- **Phase 0B — Module extraction**: Extracted `src/metrics.py`, `src/proxy/rewrite.py`, `src/proxy/upstream.py` from `chat.py` (1455→1280 lines). Added `find_matching_fact_ids` to protocol. Fixed `_run_extraction` to call `store_facts_batch`. Extracted `src/proxy/recall.py` for unified recall interception.
- **Phase 1 — GraphBackend protocol**: Created `src/graph/protocol.py` (29 methods, `@runtime_checkable`) and `src/graph/backend.py` (singleton: `init_backend`/`get_backend`/`close_backend`/`is_graph_ready`).
- **Phase 2 — Neo4j adapter**: Created `src/graph/neo4j_backend.py` wrapping existing graph modules. Lifespan uses `init_backend(Neo4jBackend())`.
- **Phase 3 — LadybugDB adapter**: Created `src/graph/ladybug_backend.py` with explicit schema (Session/Fact/File/Decision + edges). STRING-typed timestamps. Fixed result-shape unwrap in `_execute()`. 12/12 tests pass.
- **Phase 4 — Caller rewiring**: All 5 caller files (`proxy/session.py`, `assembler/context.py`, `trace/router.py`, `proxy/tool_injection.py`, `openai/chat.py`) migrated from direct module imports to `get_backend()`. All 8+ `neo4j_ready` checks replaced with `is_graph_ready()`. Added `graph_backend`/`ladybug_db_path` config.
- **Phase 5 — In-memory fixes**: `TraceStore` got `max_sessions` cap (1000) with LRU eviction. Hourly background cleanup loop wires `expire_sessions()`, `delete_expired_sessions()`, and `cleanup_stale_locks()`.
- **Test status**: 392/392 core tests pass (excluding 22 deferred streaming recall tests). LadybugDB: 12/12.

## 2026-05-13 — Observability Trace Contract Remediation

- **AssembledContext DTO extended**: Added `files_selected` and `decisions_selected` fields (`list[dict]`, default empty) to `AssembledContext` in `src/models/dtos.py`
- **Assembler propagation**: `assemble_context()` now populates `files_selected` and `decisions_selected` in the returned `AssembledContext` from the assembler's internal file/decision lists
- **Proxy wiring fix**: `chat.py` `set_assembly()` call now passes `files_selected=assembled.files_selected` and `decisions_selected=assembled.decisions_selected` instead of implicit `[]`
- **Dashboard rendering**: Turn detail in dashboard now renders "Files Injected" and "Decisions Injected" sections when `files_selected`/`decisions_selected` are populated
- **Test coverage**: Extended `test_set_assembly` with assertions on `files_selected`/`decisions_selected`; added `test_set_assembly_defaults`; added `TestAssembledContextDTO` class (3 tests)
- **Historical changelog backfill**: Added retrospective entry for Phase 3 remediation batch (smart tail, tiktoken, reasoning strip) that was previously missing
- **Wrapup artifact normalization**: Fixed observability dashboard wrapup metadata (added docs commit `a9fcf1b`, fixed plan path to archive, added `docs/DRAFT-graph-context-engine.md` to files changed); added remediation note to Phase 3 wrapup for backfilled changelog

## 2026-05-13 — Memory Engine Registration and Promotion Adapters (Phase 1 + 2)

- **Canonical promotion model (`src/memory/models.py`)**: `PromotionRecord`, `PromotionResult`, `EngineCapabilities`, `MemoryEngineConfig` — single payload shape emitted before adapter translation with auto-dedupe key generation
- **Memory engine registry (`src/memory/registry.py`)**: `MemoryEngineRegistry` with config-driven engine registration, lazy adapter instantiation, priority-based default resolution, and healthcheck aggregation. Supports 9 adapter types
- **Adapter base contract (`src/memory/adapters/base.py`)**: `MemoryAdapterBase` abstract class with required methods (validate_config, capabilities, healthcheck, promote_fact) and sensible defaults for optional methods (promote_batch, dedupe_lookup, list_by_source, update/delete_promoted)
- **First-party cth.mcp.memory adapter (`src/memory/adapters/cth_memory.py`)**: Promotes facts via the cth.mcp.memory HTTP REST API with auth, healthcheck, and source-attributed metadata
- **Generic HTTP adapter (`src/memory/adapters/generic_http.py`)**: Config-driven POST target for systems without bespoke integration, supports payload templates
- **Mem0 adapter (`src/memory/adapters/mem0.py`)**: Basic fact/observation write adapter via Mem0 REST API
- **Zep adapter (`src/memory/adapters/zep.py`)**: Basic memory/fact write adapter via Zep REST API
- **basic-memory (Obsidian) adapter (`src/memory/adapters/basic_memory.py`)**: Promotes facts as markdown files with YAML frontmatter in an Obsidian-compatible vault. Supports filesystem mode (direct .md writes) and API mode (via basic-memory REST API). Generates structured markdown with observations, relations, and provenance
- **claude-mem adapter (`src/memory/adapters/claude_mem.py`)**: Promotes facts as observations into the claude-mem worker service (SQLite + ChromaDB). Targets the HTTP API on port 37777
- **Cognee adapter (`src/memory/adapters/cognee.py`)**: Promotes facts via Cognee's ingest API (graph+vector memory control plane). Supports configurable dataset and delete (via `forget`)
- **OpenMemory adapter (`src/memory/adapters/openmemory.py`)**: Promotes facts via OpenMemory's cognitive memory REST API (multi-sector, temporal knowledge graph, decay & reinforcement). Supports delete and list-by-source
- **Nocturne Memory adapter (`src/memory/adapters/nocturne_memory.py`)**: Promotes facts as URI-graph nodes in Nocturne Memory's hierarchical namespace. Supports create, update, delete, and list-by-source with disclosure triggers and priority
- **Promotion service (`src/memory/promotion.py`)**: `PromotionService` with conservative promotion policy (confidence ≥0.9, fact type allowlist, multi-turn survival gate), dedupe, dry-run, batch promotion, audit trail, and stats
- **Config integration**: Added `MEMORY_ENGINES_JSON`, `PROMOTION_MIN_CONFIDENCE`, `PROMOTION_DRY_RUN` to `Settings`; auto-registers cth-memory from legacy `MEMORY_API_URL` if `MEMORY_ENGINES_JSON` is empty
- **Startup wiring**: Registry initialized in lifespan, `PromotionService` attached to `app.state.promotion_service`
- **Admin endpoints**: `GET /memory-engines`, `GET /memory-engines/{id}`, `GET /promotions`, `POST /promotions/retry/{id}` — engine health, capabilities, promotion audit, retry
- **62 tests** in `tests/test_memory_promotion.py` covering models, registry, adapter base, policy, service, dedupe, batch, audit trail, all 5 new adapters, and registry type coverage
- **398 tests passing** (up from 336)

## 2026-05-12 — Observability Dashboard and Operator Tooling

- **Trace contract (Phase 1)**: `TurnTrace` and `SessionTraceSummary` DTOs in `src/models/dtos.py` — canonical trace record per turn with request/assembly/response/extraction/recall fields, token economics, prompt payloads, and fallback reasons
- **Trace builder (`src/trace/builder.py`)**: `TraceBuilder` class that progressively populates a `TurnTrace` as the proxy processes a request — `with_request()`, `with_assembly()`, `with_response()`, `with_extraction()`, `with_recall()`
- **Trace store (`src/trace/store.py`)**: In-memory `TraceStore` with per-session indexing, turn_id lookup, session summary aggregation, bounded eviction (100 turns/session), and optional disk persistence (per-session JSONL files)
- **Trace API (`src/trace/router.py`)**: `GET /trace/sessions`, `GET /trace/sessions/{id}`, `GET /trace/turns/{turn_id}` — read-only inspection of turn-level proxy flow
- **Proxy wiring**: 18 wiring points in `src/openai/chat.py` capturing request, assembly, response, extraction, and recall events into `TurnTrace` records
- **Extraction capture**: All extraction fields now populated in TurnTrace (facts_stored, duplicates_skipped, invalidations_matched, extracted_facts) instead of placeholders
- **Disk persistence**: `TraceStore(trace_dir=...)` writes per-session JSONL files under `trace_dir/<session_id>.jsonl` with safe filename sanitization
- **TUI improvements (`scripts/live_monitor.py`)**: `--session` filter by session_id prefix, `--fold` event collapsing, inline latency/token deltas (per-session tracking), assembly mode detail sub-lines, trace artifact links (`/trace/sessions/{sid}/turns?turn={turn}`)
- **Web dashboard (`src/static/dashboard.html`)**: Single-page HTML dashboard at `/dashboard/` with overview metrics, session list, session detail with turn timeline, prompt diff (original vs rewritten), fact inspector, token economics, and latency bars. Zero build step — vanilla HTML/CSS/JS calling `/trace/*` and `/metrics` endpoints
- **Static file serving**: `main.py` mounts `/dashboard/` to `src/static/` and redirects `/` to `/dashboard/dashboard.html`
- **Fact graph explorer API**: 5 new endpoints in `src/trace/router.py` — `GET /trace/graph/{sid}/facts` (with fact_type/confidence/turn filters), `GET /trace/graph/{sid}/invalidations`, `GET /trace/graph/{sid}/files`, `GET /trace/graph/{sid}/decisions`, `GET /trace/graph/{sid}/recall`
- **Extraction QA workbench**: `POST /trace/qa/extract` — run extraction against sample input without full proxy replay; returns raw result, dedup check, invalidation candidates, and estimated graph write set
- **6 new tests**: disk persistence (4) + extraction builder (2) in `tests/test_trace/test_unit.py`
- **336 tests passing** (up from 330)

## 2026-05-12 — Bug Fixes: Fact Invalidation, Metrics Modes

- **P1 — Fact invalidation was a no-op**: The extractor returns description strings like "The build error on line 42 was fixed" in the `invalidated` list, but `invalidate_facts()` tried to match them against hex `fact_id` values — which never matched. Stale facts accumulated instead of being retired. Fix: added `find_matching_fact_ids()` in `facts.py` that uses Jaccard similarity (threshold 0.60) to match description strings to active fact IDs, then invalidates those. The chat.py write path now calls `find_matching_fact_ids()` before `invalidate_facts()`.
- **P2 — /metrics dropped two assembly modes**: `skipped_low_tokens` and `skipped_low_savings` weren't in the `_metrics["assembly_modes"]` initialization dict, so `_record_assembly_mode()` silently dropped them. Both modes now tracked in metrics.
- **P2 (query-rewrite) — false positive**: Reviewed the alleged `UnboundLocalError` risk in `context.py`. The `rewritten` variable is only assigned and read inside `if needs_rewrite()`, so no error occurs when the condition is false. No code change needed.
- **10 new invalidation tests** in `tests/test_graph/test_fact_invalidation.py`. **301 tests passing**.

## 2026-05-12 — Docs: Product Description + Technical Thesis

- **Architecture framing updated**: Added a concise project description and a technical thesis to `.agent/architecture.md`, clarifying that the project is a continual context curation proxy rather than just a graph-memory experiment.
- **Technical draft sharpened**: Added `Executive Summary` and `Design Thesis` sections to `docs/DRAFT-graph-context-engine.md` so the design doc now states the project goal in plain language: lower token spend, less destructive compaction, and better continuity by replacing append-only replay with a curated working set.
- **Novelty language tightened**: Reframed novelty as an architectural integration pattern — harness-agnostic proxy + session-local curated memory + off-path extraction — rather than overclaiming algorithmic novelty.

## 2026-05-12 — Extraction Quality Remediation

- **Extraction prompt rewrite**: `SYSTEM_PROMPT` rewritten with tool_result as first-listed fact type, two explicit rules requiring RESULTS-not-INTENT extraction (Rules 1-2), bad/good examples with concrete output, and a BAD Example section showing what NOT to extract (e.g., "User wants to explore yawn.frontend" → BAD, "Frontend has 14 .tsx files, uses React 18" → GOOD).
- **EXAMPLE_PROMPT expanded**: Added second example demonstrating tool-result extraction from Glob output, plus a BAD Example section with correct/incorrect contrast.
- **Fact deduplication module (`src/extractor/dedup.py`)**: Jaccard token-overlap similarity check (threshold 0.85) to prevent storing duplicate or near-duplicate facts across turns. Functions: `jaccard_similarity()`, `is_duplicate()`, `deduplicate_facts()`, `_normalize()`, `_tokenize()`.
- **Dedup wired into `_run_extraction`**: Before storing extracted facts, fetches existing active facts from the session graph, runs `deduplicate_facts()`, and stores only unique facts. Logs dedup stats when duplicates are removed.
- **coherence_tail_size raised from 3 to 10**: The previous value of 3 was too aggressive for agent conversations, destroying tool-call continuity and losing important recent context.
- **33 new dedup tests** in `tests/test_extractor/test_dedup.py`. **291 tests passing**.

## 2026-05-12 — Savings-Ratio Gate for Context Assembly

- **Problem**: The context engine was too aggressive for short/moderate conversations (5-20 messages). With `coherence_tail_size=3` and 4.3% token savings, it destroyed agent continuity (tool call history, prior decisions) while barely saving any tokens.
- **`assembly_min_input_tokens` (default: 50000)**: Don't rewrite conversations below 50K input tokens. Short conversations fit entirely in the model's context window — passthrough is strictly better.
- **`assembly_min_savings_ratio` (default: 0.20)**: Even for larger conversations, skip rewriting if savings < 20%. A 4% savings rate doesn't justify destroying the middle of the conversation.
- **Two new assembly modes**: `skipped_low_tokens` (input under floor) and `skipped_low_savings` (savings ratio under threshold). Both revert to passthrough, preserving the full conversation history.
- **Metrics**: New modes tracked in `_metrics["assembly_modes"]`.
- **258 tests passing**.

## 2026-05-12 — Live Stream WebSocket (Phase 6)

- **LiveStream module (`src/proxy/live.py`)**: Process-level pub/sub hub using `asyncio.Queue` per subscriber. Supports `broadcast()`, `subscribe()`, `unsubscribe()`. Slow consumers (queue overflow) are auto-dropped with a `dropped` sentinel. Module singleton via `get_live_stream()`/`reset_live_stream()`.
- **Convenience broadcast functions**: `broadcast_request()`, `broadcast_assembly()`, `broadcast_response()`, `broadcast_extraction()`, `broadcast_session_event()`, `broadcast_recall()` — all skip serialization work when `subscriber_count == 0` for zero-overhead when no one is watching.
- **chat.py integration**: 10 broadcast call sites wired in — request (1), assembly (1), session events (session_created + goal_updated = 2), response (streaming + non-streaming = 2), extraction (1), recall (streaming×2 + non-streaming = 3). Streaming `broadcast_response` fires after stream completes; non-streaming fires after recall interception.
- **WebSocket endpoint (`/ws/stream`)**: Added to `main.py` inside `create_app()`. Clients connect, receive JSON events in real-time. Disconnected on overflow or client disconnect.
- **LiveStream initialization**: Added to lifespan in `create_app()`, stored on `app.state.live_stream`.
- **Terminal monitor (`scripts/live_monitor.py`)**: Color-coded terminal client that connects to the WebSocket, filters event types, and displays real-time proxy activity. Supports `--filter`, `--verbose`, `--reconnect`.
- **21 new tests**: `TestLiveStreamCore` (8), `TestSingleton` (3), `TestConvenienceBroadcasts` (10) in `tests/test_proxy/test_live_stream.py`.
- **258 tests passing** (up from 237).

## 2026-05-12 — Session Goal Extraction

- **ExtractionResult.session_goal**: New field on the DTO to carry the extracted session goal from the extraction model.
- **Extraction prompt**: Updated `SYSTEM_PROMPT` with `session_goal` in the output schema, "Session Goal" extraction rules, and example output. Rule 9 added: "ALWAYS extract a session_goal, even if you can only make a rough inference."
- **client.py**: `_parse_extraction_response()` now extracts `session_goal` from the model's JSON response and passes it to `ExtractionResult`.
- **build_extraction_prompt()**: Accepts `session_goal` parameter; if provided, includes "Session goal: X" in the prompt so the extraction model can refine it.
- **chat.py — initial goal**: On new sessions, the first user message is truncated to a single-sentence goal and set via `session_repo.update_goal()` immediately. This ensures `assemble_context()` never returns `None` due to empty goal on cold start.
- **chat.py — goal fetch**: After session resolution, the current session goal is fetched from Neo4j for extraction context.
- **chat.py — goal pass-through**: `session_goal` parameter added to `_handle_streaming`, `_handle_non_streaming`, and `_run_extraction`. All three `_run_extraction` call sites (streaming, recall stream, non-streaming, recall non-streaming) pass `session_goal`.
- **_run_extraction**: Calls `extract_facts()` with `session_goal`, then calls `session_repo.update_goal()` if the extractor returns a refined goal.
- **Bootstrap loop fix**: Root cause was `goal: null` on new sessions — assemble_context returned None, so no graph context was assembled, and the agent looped MCP memory bootstrap. With initial goal from turn 1, assembly always has a goal anchor.
- **237 tests passing**.

## 2026-05-12 — Streaming Recall Interception (Phase 5b)

- **StreamingToolCallAccumulator (`src/proxy/streaming.py`)**: Reassembles streaming `delta.tool_calls` fragments into complete tool_call objects matching the non-streaming format. Tracks first_tool_name for early decision-making.
- **`stream_with_recall_detection()`**: Buffer-and-decide async generator that buffers SSE chunks until the model's intent is known (content vs tool call vs recall tool call), then either flushes buffer for passthrough or yields a StreamingRecallResult for recall processing. 5s decision timeout.
- **`_assemble_streaming_response()`**: Converts buffered streaming chunks + accumulator into a non-streaming-style response dict for recall re-send.
- **`_non_streaming_to_sse()`**: Converts a non-streaming response dict back to SSE-format lines for relaying to the client after the recall re-send.
- **Integration into `_handle_streaming()` in `chat.py`**: When `recall_injected` and `session_id`, uses `stream_with_recall_detection()` instead of `stream_with_capture()`. On recall detection: extracts question from tool call, executes recall, builds tool result, re-sends as non-streaming, handles up to 2 recall calls per turn, then converts final response to SSE format. Falls back to normal passthrough if no recall detected.
- **Double-yield bug fix**: In `stream_with_recall_detection()`, the first content line that triggers the passthrough decision was being yielded twice (once in the buffer flush, once in the passthrough section). Fixed by adding `continue` after flushing the buffer.
- **Test lifecycle fix**: Integration tests now use `httpx.MockTransport()` for upstream mock and `client.stream()` context manager to ensure the response body is consumed before the lifespan context closes the http_client.
- **21 new tests**: TestStreamingToolCallAccumulator (6), TestParseSSELine (5), TestAssembleStreamingResponse (2), TestNonStreamingToSSE (2), TestStreamWithRecallDetection (4), TestStreamingRecallInterception (2).
- **236 tests passing** (up from 215).
