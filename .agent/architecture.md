# archolith-context — Architecture

## Overview

An OpenAI-compatible proxy that transparently replaces linear conversation replay
with graph-assembled context for AI coding agents. Any harness that supports a
base URL override (Reasonix, Claude Code, Aider, Cursor, etc.) works unchanged.

## Project Description

`archolith-context` is a transparent OpenAI-compatible proxy for coding agent sessions
that replaces append-only transcript replay with LLM-driven context curation. Instead
of resending stale conversation history, it extracts durable session facts and file
content into a local knowledge store, then uses a dedicated Context Manager LLM to
build the minimum viable context window for each turn. The goal is lower token spend,
better continuity in long coding sessions, and — critically — a coding agent that
never re-reads a file it already knows, never loses a decision it has already made,
and never degrades from context bloat.

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
| Graph database (primary) | LadybugDB (embedded, file-backed, zero infra) |
| Graph database (alt.) | Neo4j (label-based isolation in default database) |
| Graph framework | Graphiti (temporal knowledge graph) |
| Fact extraction | gpt-4.1-mini (OpenAI API, cheap tier) |
| Context Manager LLM | Any OpenAI-compatible model (gpt-4.1-mini default; configurable per-deployment) |
| File content cache | LadybugDB FileContent table — SHA-256 dedup, 1-indexed line retrieval |
| Embeddings | text-embedding-3-small (OpenAI API) |
| Upstream API | Any OpenAI-compatible backend (DeepSeek, OpenAI, Anthropic adapter) |

## Data Flow

```
Harness → POST /v1/chat/completions → Proxy
│
├─ ON REQUEST:
│ 1. Parse incoming messages array
│ 2. Preserve: system prompt + last N messages (coherence tail)
│ 3. If CURATOR_ENABLED and turn >= cold_start_turns:
│    a. Run Context Manager LLM loop (≤4 tool calls, 6s budget)
│    b. LLM retrieves file sections, facts, decisions via 7 tools
│    c. Returns CuratorResult → assembled as system message
│    d. On timeout/failure: fall through to heuristic assembler
│ 4. Heuristic assembler (fallback or when curator disabled):
│    a. Query session graph: goal, active files, decisions, relevant facts
│    b. Score and budget facts to CONTEXT_TOKEN_BUDGET tokens
│ 5. Forward curated payload to real upstream API
│
├─ ON RESPONSE:
│ 6. Stream response back to harness (unchanged)
│ 7. Async: extract facts from response + tool results
│ 8. Store facts in session graph with temporal edges
│ 8b. Cache file content: pair tool_call_id → file path → content (SHA-256 dedup)
│     Update FileContent table; skip if content hash unchanged
│ 9. Invalidate superseded facts
│
└─ LIFECYCLE:
  - Session created on first request (keyed by conversation fingerprint)
  - Sessions expire after configurable TTL (default 24h)
  - Optional: promote high-confidence facts to long-term memory
```

## Component Breakdown

### Proxy Layer (`archolith_proxy/proxy/`)
- FastAPI app mimicking OpenAI `/v1/chat/completions`
- SSE streaming pass-through
- Session identification (fingerprint from system prompt + first user message)
- Request rewriting (linear → graph-assembled)
- Response capture (for post-hoc extraction)

### Context Assembler (`archolith_proxy/assembler/`)
- Queries session graph for relevant facts given current user intent
- Budgets output to configurable token limit (default ~15K)
- Always includes: session goal, active files, recent decisions
- Formats as synthetic assistant/system messages for the upstream model

### Curator LLM (`archolith_proxy/curator/`)

The primary assembly path when `CURATOR_ENABLED=true`. A tool-calling LLM that
builds the context block for each turn by actively querying the session's
knowledge store.

**Entry point:** `curate_context()` in `curator/__init__.py`
- Gated by: `CURATOR_ENABLED`, `FILE_CACHE_ENABLED`, cold-start turn count
- Model/URL/key resolved from `CURATOR_*` settings → fall back to extractor settings
- Hard latency cap: `asyncio.wait_for(loop, timeout=CURATOR_LATENCY_BUDGET_MS/1000)`
- Returns `AssembledContext` on success, `None` on timeout/failure (triggers fallback)

**Loop:** `_run_curator_native()` in `curator/loop.py`
- Up to `CURATOR_MAX_ITERATIONS` (default 4) tool-call iterations
- Stuck-loop detection: 4-wide error window, aborts on repeated identical calls
- Nous XML fallback (`_run_curator_nous()`) for models without native tool calling
- Exponential backoff with Retry-After header handling

**Result type:** `CuratorResult` in `curator/result.py`
```python
@dataclass
class CuratorResult:
    context_text: str       # formatted context block (system message content)
    curated_paths: set[str] # file paths the LLM selected
    tool_calls_used: int    # number of tool calls made
    estimated_tokens: int   # rough token estimate of context_text
```

**7 curator tools** (`curator/tools.py`):

| Tool | What it returns |
|------|----------------|
| `list_session_files` | Markdown table: path, lines, last-turn |
| `get_file` | Full content (≤200 lines) or 10-line preview + hint to use `get_file_lines` |
| `get_file_lines` | 1-indexed line slice with line numbers; clamps to EOF |
| `search_facts` | Keyword substring match over active facts, up to 20 results |
| `get_session_goal` | Session goal string |
| `get_recent_decisions` | Numbered list of decisions with turn numbers |
| `get_touched_files` | Path / status / turn table for all files touched in session |

**System prompt** (`curator/prompts.py`):
- Rules: prefer `get_file_lines` over `get_file` for files >50 lines, 2–4 tool calls max
- Output format: structured block with `=== SESSION GOAL ===`, `=== RELEVANT CODE ===`, `=== KEY FACTS ===`, `=== DECISIONS ===`

### File Content Cache (LadybugDB `FileContent` table)

Populated during `_run_extraction()` via `_extract_file_reads()` + `_upsert_file_cache()`:
- `_extract_file_reads(messages)`: pairs tool results to their originating tool calls via
  `tool_call_id` — extracts `{path, content}` with no ordering inference
- `_upsert_file_cache(session_id, file_reads, turn)`: SHA-256 dedup (skip write if hash
  unchanged), max-file-bytes guard (`FILE_CACHE_MAX_FILE_BYTES`), upsert into `FileContent` table

**`FileContent` node schema:**
```
file_id (PK)           STRING  — "{session_id}:{path}"
session_id             STRING
path                   STRING
content                STRING  — full file text
sha256                 STRING  — hex digest for change detection
line_count             INT64   — precomputed for get_file_lines efficiency
last_updated_turn      INT64
created_at             TIMESTAMP
```

Cache methods on `LadybugBackend`: `upsert_file_content`, `get_file_content`,
`get_file_lines`, `list_cached_files`. Neo4j backend has stub implementations
(returns None/[]) — file cache is LadybugDB-only in MVP.

### Fact Extractor (`archolith_proxy/extractor/`)
- Calls cheap model (gpt-4.1-mini) to parse assistant responses + tool results
- Extracts: entities, relationships, decisions, state changes
- Produces structured facts with temporal metadata
- Runs async (off critical path, concurrent with user think time)

### Session Graph (`archolith_proxy/graph/`)
- Neo4j driver targeting `context_sessions` database (isolated from memory)
- Graphiti client for temporal entity/edge management
- Session lifecycle: create, query, invalidate, expire, promote
- Node types: File, Function, Decision, Error, Goal, ToolResult, State

### Config (`archolith_proxy/config.py`)
- Upstream API URL and credentials (validated: must be http/https)
- Neo4j connection (separate from long-term memory)
- Extraction model selection
- Token budgets, TTL, coherence tail size
- Cold start turns gate (user-turn count is authoritative; token threshold is retained as a compatibility setting)
- Retry settings: upstream and Neo4j (max retries, backoff base seconds)
- Promotion settings (if wired to long-term memory)
- Memory engine config (JSON array of engine definitions)
- Promotion policy defaults (min confidence, dry-run mode)
- Settings singleton caching (get_settings / reset_settings)

### Memory Engine & Promotion (`archolith_proxy/memory/`)
- **Registry** (`registry.py`): Config-driven engine registration, lazy adapter instantiation, priority-based default resolution
- **Canonical models** (`models.py`): `PromotionRecord`, `PromotionResult`, `EngineCapabilities`, `MemoryEngineConfig`
- **Adapter base** (`adapters/base.py`): Abstract contract — validate_config, capabilities, healthcheck, promote_fact, optional batch/dedupe/CRUD
- **Concrete adapters** (`adapters/`): cth_mcp_memory, mem0, zep, generic_http
- **Promotion service** (`promotion.py`): Policy layer (confidence threshold, fact type allowlist, multi-turn survival), dedupe, dry-run, audit trail

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
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_API_KEY=sk-...

# Extraction model
EXTRACTOR_BASE_URL=https://api.openai.com/v1
EXTRACTOR_API_KEY=sk-...
EXTRACTOR_MODEL=gpt-4.1-mini

# Embeddings
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
EMBEDDING_MODEL=text-embedding-3-small

# Session graph backend
GRAPH_BACKEND=ladybug
LADYBUG_DB_PATH=./data/context.lbug

# Neo4j alternative (only when GRAPH_BACKEND=neo4j)
SESSION_NEO4J_URI=bolt://localhost:7687
SESSION_NEO4J_DATABASE=neo4j
SESSION_NEO4J_USER=neo4j
SESSION_NEO4J_PASSWORD=...

# Proxy settings
PROXY_PORT=9800
COHERENCE_TAIL_SIZE=10
MAX_TAIL_MESSAGES=20
CONTEXT_TOKEN_BUDGET=15000
SESSION_TTL_HOURS=24
COLD_START_TURNS=3
COLD_START_TOKEN_THRESHOLD=20000

# File content cache
FILE_CACHE_ENABLED=true
FILE_CACHE_MAX_FILE_BYTES=500000  # skip caching files larger than this

# Context Manager LLM (curator)
CURATOR_ENABLED=false             # disabled by default; enable to activate LLM-driven assembly
CURATOR_MODEL=                    # defaults to EXTRACTOR_MODEL if empty
CURATOR_BASE_URL=                 # defaults to EXTRACTOR_BASE_URL if empty
CURATOR_API_KEY=                  # defaults to EXTRACTOR_API_KEY if empty
CURATOR_MAX_ITERATIONS=4
CURATOR_LATENCY_BUDGET_MS=6000    # hard timeout; falls back to heuristic on expiry

# Retry / resilience
UPSTREAM_MAX_RETRIES=3
UPSTREAM_RETRY_BACKOFF_BASE_S=0.5
NEO4J_MAX_RETRIES=3
NEO4J_RETRY_BACKOFF_BASE_S=1.0

# Optional: promotion to long-term memory
MEMORY_API_URL=http://localhost:8200
MEMORY_API_KEY=...
PROMOTION_ENABLED=false

# Memory engine configuration (JSON array)
MEMORY_ENGINES_JSON=[{"id":"cth-memory","type":"cth_mcp_memory","enabled":true,"priority":10,"base_url":"http://localhost:8200","api_key_env":"MEMORY_API_KEY"}]
PROMOTION_MIN_CONFIDENCE=0.9
PROMOTION_DRY_RUN=false
```

## Observability (Phase 4)

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Health check: Neo4j status, upstream status, version, uptime |
| `GET /metrics` | Process-level counters: total_requests, assembly_modes, extraction_successes/empties/failures, upstream_errors, neo4j_errors, active_sessions, token_savings_estimated, total_input_tokens_seen, trace_records, trace_sessions, uptime, curator_calls, curator_timeouts, curator_fallbacks |
| `GET /sessions` | List active sessions (admin, 503 if Neo4j down) |
| `GET /sessions/{id}` | Session stats (admin, 404 if not found, 503 if Neo4j down) |
| `GET /trace/sessions` | List all sessions with trace records |
| `GET /trace/sessions/{id}` | Session trace summary + turns (limit/offset pagination) |
| `GET /trace/turns/{turn_id}` | Single turn trace by turn_id |
| `GET /trace/graph/{sid}/facts` | Session facts with filters: fact_type, min_confidence, from_turn, to_turn, include_invalidated |
| `GET /trace/graph/{sid}/invalidations` | Supersession/invalidation chains for a session |
| `GET /trace/graph/{sid}/files` | Files touched by a session (via TOUCHES edges) |
| `GET /trace/graph/{sid}/decisions` | Decisions recorded for a session |
| `GET /trace/graph/{sid}/recall` | Recall events from trace records |
| `POST /trace/qa/extract` | Extraction QA workbench — run extraction without full proxy replay; dedup and invalidation checks now route through the active backend (`ladybug` or `neo4j`) |
| `GET /memory-engines` | List configured memory engines with health status |
| `GET /memory-engines/{id}` | Single engine details, health, and capabilities |
| `GET /promotions` | Promotion history and stats |
| `POST /promotions/retry/{id}` | Retry a failed promotion |
| `GET /dashboard/` | Web dashboard (single-page HTML, zero build step) |
| `GET /ws/stream` | WebSocket live event stream |

Metrics are in-memory (`_metrics` dict surfaced via `archolith_proxy/metrics.py`), reset on process restart. Prometheus-compatible OpenMetrics format is a future goal.

`assembly_modes` tracks: `graph`, `fallback`, `cold_start`, `passthrough`, `curator`.

## Resilience (Phase 4)

### Upstream Retry
`upstream_request_with_retry()` in `archolith_proxy/proxy/upstream.py`:
- Exponential backoff on 429, 500, 502, 503, 504 + `TimeoutException` + `ConnectError`
- Configurable: `UPSTREAM_MAX_RETRIES` (default 3), `UPSTREAM_RETRY_BACKOFF_BASE_S` (default 0.5s)

### Neo4j Startup Retry
`_init_neo4j_with_retry()` in `archolith_proxy/main.py`:
- Exponential backoff on connection failure at startup
- Configurable: `NEO4J_MAX_RETRIES` (default 3), `NEO4J_RETRY_BACKOFF_BASE_S` (default 1.0s)
- If all retries fail, proxy starts without graph features (passthrough mode)

### Graceful Degradation
- Assembler wraps each graph query in try/except → returns None on failure
- Chat handler tracks `assembly_mode`: `cold_start` (below threshold), `graph` (success), `curator` (curator-assembled), `fallback` (graph failed, used passthrough), `passthrough` (Neo4j not configured)
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