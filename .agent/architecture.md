# cth.context-engine — Architecture

## Overview

An OpenAI-compatible proxy that transparently replaces linear conversation replay
with graph-assembled context for AI coding agents. Any harness that supports a
base URL override (Reasonix, Claude Code, Aider, Cursor, etc.) works unchanged.

## Project Description

`cth.context-engine` is a transparent OpenAI-compatible proxy for coding agents
that replaces append-only transcript replay with continual context curation.
Instead of repeatedly resending stale conversation history, it extracts durable
session facts, maintains a curated working memory, and reconstructs only the
context needed for the next turn. The goal is lower token spend, less
destructive compaction, and better continuity in long-running coding sessions.

## Technical Thesis

The system is built around a simple claim: linear transcript replay is the wrong
data structure for long coding sessions.

- Standard agent clients treat context as an append-only log and pay the cost of
  resending large amounts of stale history on every turn.
- This project treats context as a continuously curated working set: preserve the
  local coherence tail, extract durable state off-path, invalidate superseded
  facts, and rebuild the middle of the prompt from session state instead of raw
  replay.
- Token savings is the measurable outcome, but the deeper architectural goal is
  to make prompt compaction less necessary by keeping the session state curated
  continuously rather than waiting for the transcript to overflow.
- The proxy shape matters: by enforcing this curation layer at the API boundary,
  the system can improve existing harnesses without requiring each agent runtime
  to adopt a custom memory SDK or internal framework.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Proxy server | Python, FastAPI, uvicorn |
| Graph database | Neo4j (label-based isolation: `:ContextSession` label in default `neo4j` database) |
| Graph framework | Graphiti (temporal knowledge graph) |
| Fact extraction | gpt-4.1-mini (OpenAI API, cheap tier) |
| Embeddings | text-embedding-3-small (OpenAI API) |
| Upstream API | Any OpenAI-compatible backend (DeepSeek, OpenAI, Anthropic adapter) |

## Data Flow

```
Harness → POST /v1/chat/completions → Proxy
  │
  ├─ ON REQUEST:
  │   1. Parse incoming messages array
  │   2. Preserve: system prompt + last N messages (coherence tail)
  │   3. Replace middle messages with graph-assembled context
  │   4. Query session graph: goal, active files, decisions, relevant facts
  │   5. Forward curated payload to real upstream API
  │
  ├─ ON RESPONSE:
  │   6. Stream response back to harness (unchanged)
  │   7. Async: extract facts from response + tool results
  │   8. Store facts in session graph with temporal edges
  │   9. Invalidate superseded facts
  │
  └─ LIFECYCLE:
      - Session created on first request (keyed by conversation fingerprint)
      - Sessions expire after configurable TTL (default 24h)
      - Optional: promote high-confidence facts to long-term memory
```

## Component Breakdown

### Proxy Layer (`src/proxy/`)
- FastAPI app mimicking OpenAI `/v1/chat/completions`
- SSE streaming pass-through
- Session identification (fingerprint from system prompt + first user message)
- Request rewriting (linear → graph-assembled)
- Response capture (for post-hoc extraction)

### Context Assembler (`src/assembler/`)
- Queries session graph for relevant facts given current user intent
- Budgets output to configurable token limit (default ~15K)
- Always includes: session goal, active files, recent decisions
- Formats as synthetic assistant/system messages for the upstream model

### Fact Extractor (`src/extractor/`)
- Calls cheap model (gpt-4.1-mini) to parse assistant responses + tool results
- Extracts: entities, relationships, decisions, state changes
- Produces structured facts with temporal metadata
- Runs async (off critical path, concurrent with user think time)

### Session Graph (`src/graph/`)
- Neo4j driver targeting `context_sessions` database (isolated from memory)
- Graphiti client for temporal entity/edge management
- Session lifecycle: create, query, invalidate, expire, promote
- Node types: File, Function, Decision, Error, Goal, ToolResult, State

### Config (`src/config/`)
- Upstream API URL and credentials (validated: must be http/https)
- Neo4j connection (separate from long-term memory)
- Extraction model selection
- Token budgets, TTL, coherence tail size
- Cold start thresholds (turns + token count)
- Retry settings: upstream and Neo4j (max retries, backoff base seconds)
- Promotion settings (if wired to long-term memory)
- Settings singleton caching (get_settings / reset_settings)

## Isolation from Long-term Memory

| | Long-term memory (cth.mcp.memory) | Session context (this project) |
|--|-----------------------------------|-------------------------------|
| Neo4j database | `neo4j` (default) | `neo4j` (same, label-based isolation) |
| Isolation | `:Memory` label on all nodes | `:ContextSession` label on all nodes |
| Lifecycle | Persistent, decays over months | Ephemeral, TTL per session |
| Write path | Agent stores explicitly | Proxy extracts automatically |
| Read path | MCP tools (recall, build_context) | Proxy assembler (internal) |

No shared indices, no cross-contamination. All queries are label-scoped (`MATCH (n:ContextSession ...)` vs `MATCH (n:Memory ...)`). Session data is bulk-droppable by label (`MATCH (n:ContextSession) DETACH DELETE n`).

## Configuration / Environment Variables

```env
# Upstream API (what the proxy forwards to)
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
UPSTREAM_API_KEY=sk-...

# Extraction model
EXTRACTOR_BASE_URL=https://api.openai.com/v1
EXTRACTOR_API_KEY=sk-...
EXTRACTOR_MODEL=gpt-4.1-mini

# Embeddings
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-small

# Session graph (Neo4j — label-based isolation in default database)
SESSION_NEO4J_URI=bolt://localhost:7687
SESSION_NEO4J_DATABASE=neo4j
SESSION_NEO4J_USER=neo4j
SESSION_NEO4J_PASSWORD=...

# Proxy settings
PROXY_PORT=9800
COHERENCE_TAIL_SIZE=3
MAX_TAIL_MESSAGES=20
CONTEXT_TOKEN_BUDGET=15000
SESSION_TTL_HOURS=24
COLD_START_TURNS=3
COLD_START_TOKEN_THRESHOLD=20000

# Retry / resilience
UPSTREAM_MAX_RETRIES=3
UPSTREAM_RETRY_BACKOFF_BASE_S=0.5
NEO4J_MAX_RETRIES=3
NEO4J_RETRY_BACKOFF_BASE_S=1.0

# Optional: promotion to long-term memory
MEMORY_API_URL=http://localhost:8200
MEMORY_API_KEY=...
PROMOTION_ENABLED=false
```

## Observability (Phase 4)

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Health check: Neo4j status, upstream status, version, uptime |
| `GET /metrics` | Process-level counters: total_requests, assembly_modes (cold_start/graph/fallback/passthrough), extraction_successes/failures, upstream_errors, neo4j_errors, active_sessions, token_savings_estimated, total_input_tokens_seen, uptime |
| `GET /sessions` | List active sessions (admin, 503 if Neo4j down) |
| `GET /sessions/{id}` | Session stats (admin, 404 if not found, 503 if Neo4j down) |

Metrics are in-memory (`_metrics` dict in `src/main.py`), reset on process restart. Prometheus-compatible OpenMetrics format is a future goal.

## Resilience (Phase 4)

### Upstream Retry
`_upstream_request_with_retry()` in `src/openai/chat.py`:
- Exponential backoff on 429, 500, 502, 503, 504 + `TimeoutException` + `ConnectError`
- Configurable: `UPSTREAM_MAX_RETRIES` (default 3), `UPSTREAM_RETRY_BACKOFF_BASE_S` (default 0.5s)

### Neo4j Startup Retry
`_init_neo4j_with_retry()` in `src/main.py`:
- Exponential backoff on connection failure at startup
- Configurable: `NEO4J_MAX_RETRIES` (default 3), `NEO4J_RETRY_BACKOFF_BASE_S` (default 1.0s)
- If all retries fail, proxy starts without graph features (passthrough mode)

### Graceful Degradation
- Assembler wraps each graph query in try/except → returns None on failure
- Chat handler tracks `assembly_mode`: `cold_start` (below threshold), `graph` (success), `fallback` (graph failed, used passthrough), `passthrough` (Neo4j not configured)
- Extraction failures logged + counted, never block the response
- Proxy returns 503 if lifespan didn't initialize `http_client`

### Config Validation
- `field_validator` on `upstream_base_url` (must be http/https), `proxy_port` (1–65535)
- Warning log on empty `upstream_api_key`
- `check_required_for_graph()` → list of missing vars for graph features
- `check_required_for_proxy()` → list of missing vars for basic proxy
- Settings singleton via `get_settings()` + `reset_settings()` for tests

## Docker Deployment (Phase 4)

- `Dockerfile`: python:3.12-slim + uv, healthcheck on `/health`, uvicorn CMD
- `docker-compose.yml`: proxy + neo4j:5-community with APOC plugin, healthchecks, volumes, dependency ordering
- Override file: `docker-compose.override.yml` (gitignored)

## External Dependencies

| Service | Purpose | Required |
|---------|---------|----------|
| Neo4j | Session graph storage | Yes (graceful fallback if down) |
| OpenAI API (gpt-4.1-mini) | Fact extraction | Yes (extraction skipped on failure) |
| OpenAI API (embeddings) | Semantic similarity for retrieval | Yes (future) |
| Upstream LLM API | Target for proxied requests | Yes |
| cth.mcp.memory API | Promotion target for durable facts | Optional |

## Port Assignment

| Service | Port |
|---------|------|
| Context Engine Proxy | 9800 |
| Neo4j (shared instance, label-isolated) | 7687 |
