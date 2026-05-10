# cth.context-engine — Architecture

## Overview

An OpenAI-compatible proxy that transparently replaces linear conversation replay
with graph-assembled context for AI coding agents. Any harness that supports a
base URL override (Reasonix, Claude Code, Aider, Cursor, etc.) works unchanged.

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
- Upstream API URL and credentials
- Neo4j connection (separate from long-term memory)
- Extraction model selection
- Token budgets, TTL, coherence tail size
- Promotion settings (if wired to long-term memory)

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
UPSTREAM_BASE_URL=https://api.deepseek.com
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
CONTEXT_TOKEN_BUDGET=15000
SESSION_TTL_HOURS=24

# Optional: promotion to long-term memory
MEMORY_API_URL=http://localhost:8200
MEMORY_API_KEY=...
PROMOTION_ENABLED=false
```

## External Dependencies

| Service | Purpose | Required |
|---------|---------|----------|
| Neo4j | Session graph storage | Yes |
| OpenAI API (gpt-4.1-mini) | Fact extraction | Yes |
| OpenAI API (embeddings) | Semantic similarity for retrieval | Yes |
| Upstream LLM API | Target for proxied requests | Yes |
| cth.mcp.memory API | Promotion target for durable facts | Optional |

## Port Assignment

| Service | Port |
|---------|------|
| Context Engine Proxy | 9800 |
| Neo4j (shared instance, different DB) | 7687 |
