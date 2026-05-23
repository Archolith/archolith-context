# Changelog — cth.context-engine

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
