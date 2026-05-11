# Changelog — cth.context-engine

## 2026-05-11 — Session Recall as Proxy-Intercepted Tool (Step 13)

- **Session recall tool (`src/proxy/tool_injection.py`)**: New module that injects a synthetic `__context_engine_recall` tool into requests when a session is active. When the model calls this tool, the proxy intercepts the call, queries the session graph for relevant facts, and returns the results as a tool response — then re-sends to upstream for the model to continue with the recalled context.
- **`inject_recall_tool()`**: Adds the recall tool definition to `body["tools"]` (idempotent, preserves tool_choice).
- **`strip_recall_tool()`**: Removes the recall tool from request body (for fingerprinting and re-send cleanup).
- **`find_recall_tool_call()`**: Detects a recall tool call in non-streaming responses.
- **`strip_recall_from_response()`**: Removes recall tool calls from the final response so the harness never sees internal proxy tools.
- **`handle_recall_tool_call()`**: Queries the session graph using the same retrieval pipeline as passive context assembly — embeds the question (with optional query rewriting), scores facts, budgets to 2K tokens, and formats results.
- **`build_tool_result_message()`**: Constructs a tool result message with role="tool" for the recall call.
- **Integration into `chat.py`**: Before forwarding to upstream, injects the recall tool if `session_recall_tool_enabled=true` and a session is active. After the non-streaming response, checks for recall tool calls — if found, handles the recall, builds a tool result, and re-sends to upstream with the recall results. Strips internal tool artifacts from the final response.
- **Config**: `session_recall_tool_enabled: bool = False` added to Settings (disabled by default).
- **Non-streaming only**: Streaming recall interception deferred to Phase 5b.
- **26 new tests**: TestInjectRecallTool (7), TestStripRecallTool (4), TestFindRecallToolCall (6), TestStripRecallFromResponse (5), TestBuildToolResultMessage (2), TestRecallToolConstants (2).
- **215 tests passing** (up from 189).

## 2026-05-11 — Query Rewriting for Ambiguous Messages (Step 9)

- **Query rewriting (`src/assembler/query_rewrite.py`)**: New module with `needs_rewrite()` + `rewrite_query()` — detects ambiguous user messages containing pronouns, vague directives, or deictic references and rewrites them to be self-contained before embedding. Uses the extractor model (gpt-4.1-mini) with recent conversation context for reference resolution.
- **`needs_rewrite()`**: Pattern-based detection — pronouns (`it`, `this`, `that`) and deictic references (`the previous function`, `the above code`) ALWAYS need rewriting (inherently ambiguous). Vague directives (`do it`, `continue`) and short queries only trigger when they lack specific technical keywords.
- **`rewrite_query()`**: Calls extractor model with last 3 user/assistant exchanges as context. Returns rewritten query or None on failure (graceful fallback).
- **`extract_recent_exchanges()`**: Extracts last N user/assistant message pairs from the message array, skipping system/tool messages.
- **Integration**: `assemble_context()` now accepts optional `messages` parameter; when `query_rewrite_enabled=true` and `embedding_enabled=true`, rewrites ambiguous user messages before computing the query embedding. The rewritten query is embedded instead of the raw text, improving cosine similarity matching.
- **Config**: `query_rewrite_enabled: bool = False` added to Settings (disabled by default).
- **Cost**: ~$0.0003/turn when triggered (only fires for ambiguous queries, uses cheap gpt-4.1-mini).
- **35 new tests**: TestNeedsRewrite (20), TestExtractRecentExchanges (6), TestRewriteQuery (6), TestQueryRewriteIntegration (3).
- **189 tests passing** (up from 154).

## 2026-05-11 — Phase 3 Remediation Batch: Steps 7a, 7b, 11, 12

- **Fingerprint tool normalization (Step 7a)**: New regex patterns in `_SANITIZE_PATTERNS` strip tool definition blocks (`Available tools: [...]`, `Tool definitions: [...]`) and JSON tool schema lines (`"name"`/`"description"`/`"parameters"` key-value lines) from system prompts before fingerprinting. Prevents session resets when harnesses dynamically register tools.
- **Turn-locking for extraction race condition (Step 7b)**: New `src/proxy/locks.py` with per-session `asyncio.Lock` registry. `wait_for_prior_extraction()` called before assembly (5s timeout). `_run_extraction()` holds session lock during graph writes (10s acquire timeout) with guaranteed release in `finally` block. Prevents stale graph reads when turn N+1 arrives before turn N's extraction commits.
- **Two-tier context read (Step 11)**: Split `_format_context_block()` into two sections: `=== SESSION OVERVIEW ===` (goal, files, decisions, fact count — stable across turns for prompt caching) and `=== RELEVANT CONTEXT ===` (budgeted facts — changes per-turn based on query). Overview comes first (stable prefix benefits API prompt caching).
- **Context-overflow compaction (Step 12)**: New `src/assembler/compaction.py` with `compact_context()` using the extractor model to summarize oversized context blocks. Triggered when rewritten payload exceeds `context_token_budget`. Gated behind `COMPACTION_ENABLED=true` (default false). Cost: ~$0.001/turn when triggered (rare). Falls back to oversized assembled context on failure.
- **Config**: `compaction_enabled: bool = False` added to Settings
- **Metrics**: `compaction_applied` counter added to `/metrics`
- **26 new tests**: 10 locks, 7 two-tier formatting, 5 compaction, 4 tool normalization
- **154 tests passing** (up from 128)

## 2026-05-11 — Embedding-Driven Fact Retrieval (Steps 6b-d)

- **Cosine similarity scoring**: `_cosine_similarity()` + `_score_fact()` — weighted blend: similarity(40%) + recency(30%) + type+confidence(30%) with embeddings; type(40%) + confidence(30%) + recency(30%) without
- **Context windowing**: `_expand_with_context_window()` — N-1/N+1 adjacent-turn fact expansion for narrative continuity (error→fix, question→decision)
- **Query embedding cache**: `_get_query_embedding()` with SHA-256-keyed in-memory cache (64-entry FIFO eviction)
- **Config flag**: `embedding_enabled: bool = False` — off by default, no behavior change unless configured
- **Chat handler integration**: extracts last user message from request body, passes to `assemble_context()` with `http_client` when embedding_enabled
- **Bug fix**: `_budget_facts()` default `turn_number=0` caused recency inflation; now infers `effective_turn` from `max(source_turn)` of facts
- **Bug fix**: `TestColdStartLogic` indentation — class-level asserts moved inside method body
- **16 new unit tests**: TestCosineSimilarity (6), TestScoreFact (4), TestContextWindowing (4), TestBudgetFactsWithEmbeddings (2)
- **128 tests passing** (up from 112)

## 2026-05-11 — Streaming Fix: True SSE Passthrough

- **Streaming regression fix**: `_handle_streaming()` was using `client.post()` + `resp.text.split("\n")` which fully buffered the upstream response before relaying any chunks to the client. Restored true SSE passthrough using `client.stream()` + `aiter_lines()` — the client now sees tokens in real-time as they arrive from upstream
- **Two-phase streaming architecture**:
  1. Phase 1: Connection-level retry — open `client.stream()`, check status code before committing. If 429/5xx or connection error, close and retry with backoff
  2. Phase 2: True SSE passthrough — relay `aiter_lines()` directly to client; `ResponseCapture` runs in parallel for post-hoc extraction
- **Resource cleanup**: Proper `__aexit__` calls on all code paths (retry, error, success) to prevent stream context leaks
- **3 new integration tests**: SSE passthrough content, non-retryable error relay, all retries exhausted
- **100 tests passing** (up from 97)

## 2026-05-10 — P2/P3 Fixes: Structlog, Streaming Retry, Embeddings, Metrics

- **Structlog fix**: Replaced `PrintLoggerFactory()` with `stdlib.LoggerFactory()` — `filter_by_level` requires `.disabled` attribute only present on stdlib loggers. Moved `filter_by_level` to structlog processor chain (not ProcessorFormatter which receives LogRecords where logger may be None)
- **Streaming retry**: Connection-level retry on 429/5xx/timeout/connect errors before committing to SSE stream. Once chunks flow to client, retry is impossible (documented limitation)
- **Batch embeddings**: New `src/extractor/embeddings.py` with `compute_embeddings_batch()` calling text-embedding-3-small. Facts stored with embeddings; graceful fallback to None if API unavailable
- **Metrics derived rates**: `/metrics` now includes `extraction_success_rate`, `avg_token_savings_per_request`, `token_savings_rate`
- **Request logging middleware**: Binds session context (session_id, turn_number, assembly_mode) via `structlog.contextvars` so request logs include handler-enriched context
- **Integration tests**: 18 new tests in `test_integration_phase4.py` — Neo4j chaos, extraction chaos, session isolation, structlog config, streaming retry, metrics rates, batch embeddings, request logging middleware
- **97 tests passing** (up from 79)

## 2026-05-10 — Phase 4: Hardening + Operational Readiness

- Config validation: field validators on upstream_base_url (http/https), proxy_port (1–65535); check_required_for_graph()/check_required_for_proxy() methods; settings singleton caching (get_settings/reset_settings); retry config fields
- Graceful degradation: assembler wraps each graph query in try/except returning None on failure; assembly_mode tracking (cold_start/graph/fallback/passthrough); proxy returns 503 when lifespan didn't complete; Neo4j down → passthrough
- Observability: /health enhanced with upstream status + version + uptime; /metrics with full counters (total_requests, assembly_modes, extraction_successes/failures, upstream_errors, neo4j_errors, active_sessions, token_savings_estimated, total_input_tokens_seen); /sessions + /sessions/{id} admin endpoints
- Error recovery: _upstream_request_with_retry() with exponential backoff on 429/5xx + TimeoutException/ConnectError; _init_neo4j_with_retry() at startup; extraction isolation (failures logged + counted, never block)
- Request logging middleware: method, path, status, latency_ms per request
- Performance: invalidated_at + session_id indexes on Fact, session_id index on File; invalidated_at initialized as null on fact creation
- Docker: Dockerfile (python:3.12-slim + uv, healthcheck) + docker-compose.yml (proxy + neo4j:5-community with APOC)
- Tests: 23 Phase 4 unit tests (79 total passing)

## 2026-05-09 — Phase 3: Context Assembly + Request Rewriting

- Assembler queries session graph for goal, facts, files, decisions
- Token-budgeted fact selection with priority ordering
- Message rewriting: merge graph context into system message, keep coherence tail, discard middle
- Cold start logic: passthrough until turns ≥ 3 OR input_tokens > 20K
- NVIDIA API role alternation fix: strip leading assistant/tool, merge consecutive same-role

## 2026-05-09 — Phase 2: Session Management + Fact Extraction + Neo4j

- Session resolution: X-Session-ID header primary, SHA-256 fingerprint fallback
- Fact extraction via gpt-4.1-mini with structured JSON schema
- Neo4j graph write path with label-guard isolation
- Extraction prompt improvements + parser normalization
- Bug fixes: await transaction, Content-Type header, label scoping, background tasks

## 2026-05-09 — Phase 1: Full OpenAI-Compatible Proxy

- POST /v1/chat/completions passthrough (streaming + non-streaming)
- GET /v1/models proxy
- Catch-all for unrecognized /v1/* routes
- SSE streaming with ResponseCapture (512KB buffer)
- OpenAI-formatted error responses

## 2026-05-09 — Phase 0: Project Scaffold

- Created project skeleton via cth.agentsmith scaffold
- Added technical design draft
- Filled in architecture.md and data_models.md
- Established isolation model: Neo4j label-based (:ContextSession)
