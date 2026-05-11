# Changelog — cth.context-engine

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
