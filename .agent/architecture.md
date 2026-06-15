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

**Naming reality in the live repo:**
- Public repo / product name: `archolith-context`
- Python distribution name: `archolith-proxy`
- Import/package root: `archolith_proxy`
- Historical `cth.context-engine` naming still appears in older docs, prompts, and changelog entries

## Archolith Ecosystem

archolith-context is one module in a broader end-to-end AI tooling platform.
Each module is a standalone Python library — installable independently, with
zero knowledge of its siblings.  archolith-context is the orchestration layer
that wires them together at the proxy boundary.

| Module | Role | Dependency model |
|--------|------|-----------------|
| **archolith-filter** | Token reduction — filter noise, shrink oversized tool results | Optional peer; fail-open lazy import |
| **archolith-memory** | Long-term memory — cross-session fact storage and retrieval | Optional peer; fail-open lazy import (planned) |
| **archolith-context** | Proxy — orchestrates session context, extraction, curation | Orchestrator; imports peers when available |

**Design constraint (applies to all modules):**
- Each module ships as `pip install archolith-<name>` with no mandatory dependencies on siblings
- archolith-context treats peers as optional: all peer integration paths are fail-open
- Peers have zero dependency on archolith-context and are usable standalone
- MCP servers (when they exist) are thin wrappers around the library — not the primary integration surface
- The proxy is the primary integration surface: it imports libraries directly, not via HTTP or tool calls

**archolith-memory integration shape (planned):**
- Read path: proxy queries `archolith_memory.recall(query)` before each upstream call and injects relevant long-term memories into context automatically — no agent tool call required
- Write path: proxy calls `archolith_memory.store(fact, confidence)` from the promotion pipeline for high-confidence session facts — no agent tool call required
- Explicit write: MCP server exposes `add_memory` / `add_todo` as thin wrappers for agent-initiated writes when the agent wants to tag something important
- The MCP server's recall tools (`recall_memories`, `build_context`) become optional/legacy once proxy-side injection is live

---

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
| Graph backend abstraction | `GraphBackend` protocol with Neo4j and LadybugDB implementations |
| Graph database (bootstrap-friendly) | LadybugDB (embedded, file-backed, zero infra) |
| Graph database (code default) | Neo4j (label-based isolation in default database) |
| Graph framework | _removed (Graphiti was a never-imported dependency; see dead-code-removal plan)_ |
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
│ 1. Parse incoming messages array, detect turn type
│    - User turn: last message role = "user" → full assembly path
│    - Agent-solo turn: last message role = "tool" → mechanical compression
│
│ 2a. AGENT-SOLO PATH (tool-call continuations, ~85% of requests):
│    a. Curator prefix cache: if cached rewrite exists from last user turn,
│       splice it in (count + fingerprint match, O(1) check)
│    b. RTK Layer 3 strategies (D→C→B→A):
│       D: Compact completed Write/Edit tool_use arguments
│       C: filter_output() on compressible tools in middle section
│       B: Cross-turn dedup via per-session DedupeTracker
│       A: Char-budget all tool results to max_tokens * 4 chars
│    c. Forward compressed payload to upstream
│
│ 2b. USER TURN PATH:
│    a. Preserve: system prompt + last N messages (coherence tail)
│    b. If CURATOR_ENABLED and turn >= cold_start_turns:
│       - Run Context Manager LLM loop (≤6 tool calls, 30s budget)
│       - LLM retrieves file sections, facts, decisions via 13 tools
│       - Returns CuratorResult → assembled as system message
│       - On timeout/failure: fall through to passthrough
│       - Cache rewritten messages for agent-solo prefix persistence
│    c. rewrite_messages(): merge graph context + coherence tail
│       - RTK Layer 1: filter_tool_messages() strips noise from tool-role messages
│       - RTK Layer 2: shrink_tool_call_args() collapses large Write/Edit args
│       - RTK Layer 2: shrink_tail_tool_results() caps token footprint of tail
│    d. Forward curated payload to real upstream API
│
├─ ON RESPONSE:
│ 7. Stream response back to harness (unchanged)
│ 8. Async: extract facts from response + tool results
│    a. `extraction_mode=turn_boundary` (default): LLM fact extraction runs
│       only on user-turn boundaries or when finish_reason=stop — skipping
│       the ~85% of requests that are agent-solo continuations. File-cache
│       capture (reads, writes, invalidation) always runs on every request.
│    b. `extraction_mode=every_turn`: preserves legacy behavior (extract on
│       every request).
│    c. RTK Layer 1: filter_single_tool_result() denoises each tool result
│       before packing into the 4000-char extractor budget
│ 9. Store facts in session graph with temporal edges
│ 9b. Cache file content: pair tool_call_id → file path → content (SHA-256 dedup)
│     Update FileContent table; skip if content hash unchanged
│     Also cache Write/create_file tool_call args directly (no re-read needed)
│ 10. Invalidate superseded facts
│
├─ BACKGROUND CLEANUP (periodic, every N seconds):
│  - Expire sessions past TTL
│  - Clean up old trace records (retention_days policy)
│  - Invalidate stale cached briefings
│  - Optional: compact session fact graph
│
└─ LIFECYCLE:
  - Session created on first request (keyed by conversation fingerprint)
  - Sessions expire after configurable TTL (default 24h)
  - Optional: promote high-confidence facts to long-term memory
  - Graph cleanup runs via `graph/cleanup.py` scheduled tasks
  - Trace cleanup runs via `trace/store.py` retention policy (default: keep 30 days)
```

## Component Breakdown

### Proxy Layer (`archolith_proxy/proxy/`, `archolith_proxy/openai/`, `archolith_proxy/routers/`)

**Core proxy files** (`archolith_proxy/proxy/`):

Request/Response Orchestration:
- `session.py` — session identification (fingerprint from system prompt + first user message); session state tracking and expiry
- `rewrite.py` — request rewriting orchestration (linear → graph-assembled); message array rebuild with curator results and coherence tail
- `upstream.py` — upstream API passthrough with exponential backoff retry logic; 429/500/502/503/504 + timeout resilience
- `router.py` — routing decisions and mode classification (user turn vs agent-solo vs error recovery); assembly gate logic

Streaming & Response Handling:
- `live.py` — live stream handlers for SSE/streaming response pass-through and retry handling; WebSocket broadcast for extraction events
- `streaming.py` — SSE streaming pass-through and chunk buffering; server-sent events transport for real-time response delivery

Context Assembly & Recall:
- `recall.py` — proxy-forced synthetic memory recall injection and fact-selection logic; triggers on repeated file reads or recall phrases in user messages; handles up to 2 nested recall rounds per turn
- `agent_solo.py` — agent-solo turn compression and curator prefix-cache logic (covered separately below); deterministic token reduction for tool-call continuation turns

Synthetic Tools & Injection:
- `tool_injection.py` — synthetic tool registration (memory_recall, file_read intercepts, prefetch); tool schema injection and response parsing; __archolith_recall tool call detection and handling
- `tool_intercept.py` — interception handlers for native file reads from cache; tool-call dispatch routing to synthetic handlers
- `synthetic_tools.py` — tool implementations (recall_session_work, recall_files_read, recall_file); returns structured summaries from session graph and file cache

Concurrency & Reliability:
- `locks.py` — session-level locking primitives (async semaphores) for concurrent turn handling; per-session lock acquisition to gate extraction and graph writes
- `circuit_breaker.py` — adaptive circuit breaker for upstream failures and synthetic tool fallbacks; per-session state with cooldown and hard-disable thresholds

**OpenAI-compatible layer** (`archolith_proxy/openai/`):

Entry Points & Routing:
- `chat.py` — main `/v1/chat/completions` endpoint handler; routes to streaming or non-streaming paths based on request `stream` flag; handles recall interception, synthetic tool injection, and extraction dispatch
- `router.py` — FastAPI APIRouter for `/v1/chat/completions` and `/v1/models` endpoints; registers with the main app

Response Handling:
- `streaming.py` — streaming response parsing, SSE chunk handling, delta accumulation; collects streamed completion chunks into a full response for processing; handles recall and synthetic tool injection in streaming context
- `non_streaming.py` — non-streaming response collection and finalization; validates response structure, applies filters, and prepares for extraction

Extraction & Knowledge Graph:
- `extraction.py` — post-response async extraction pipeline (off critical path); fact extraction, file content caching, invalidation logic, and promotion service dispatch; runs lock-gated per session with configurable concurrency
- `file_cache.py` — file content cache population from tool results; SHA-256 dedup, line-count precomputation, FileContent node upsert

Utilities & Schemas:
- `helpers.py` — token counting (tiktoken cl100k_base), model resolution, payload formatting utilities; message normalization, tool result filtering before extractor LLM budget
- `schemas.py` — OpenAI-compatible Pydantic models (ChatMessage, ChatCompletionRequest, Choice, Delta, etc.); request/response validation
- `models.py` — model catalog and upstream model delegation logic; resolves upstream model names to actual endpoints

Error Handling & Fallback:
- `errors.py` — error handling and upstream error translation; maps HTTP status codes to OpenAI-compatible error responses
- `passthrough.py` — identity passthrough for non-curated requests (fallback mode); returns original upstream response unchanged when assembly/curation fails

### Context Assembler (`archolith_proxy/assembler/`)

Queries session graph and briefing state for relevant facts given current user intent.

**Core modules:**
- `context.py` — main entry point: `assemble_context()` that queries graph, applies budgets, returns `AssembledContext`

> **Note:** The heuristic fact-ranking assembler (`assemble_context()` in `context.py`) serves the `__archolith_recall` tool path only. The main chat-assembly path (user-turn context rewriting) uses the Curator LLM (`curator/pipeline.py`) or passthrough fallback — not the scoring-based fact assembler.
- `intent.py` — parse current turn intent from latest user message (project/goal/action keywords)
- `query_rewrite.py` — convert intent into graph query shape (keyword expansion, synonym mapping)
- `compress.py` — apply token budgets to facts/files/decisions; handle overflow
- `tail.py` — preserve coherence tail messages (last N turns always kept, with tool-call integrity handling)

> **Known limitation (2026-06):** The smart-tail integrity validation and over-max fallback have known issues tracked in the Coherence-Tail Integrity Fixes plan. Tool-call integrity handling may nullify valid tail messages when the message count exceeds `MAX_TAIL_MESSAGES`, and the fallback path drops tool-call associations rather than preserving them. These are targeted for remediation in a separate plan.
- `compaction.py` — optional message compaction for middle section facts

**Output contract:**
- Budgets output to configurable token limit (default ~15K tokens)
- Always includes: session goal, active files, recent decisions
- Formats as synthetic assistant/system messages for the upstream model
- Returns `AssembledContext` with: `system_message`, `graph_context`, `coherence_tail`, token estimates, facts/files/decisions selected

### Curator LLM (`archolith_proxy/curator/`)

The primary assembly path when `CURATOR_ENABLED=true`. A tool-calling LLM that
builds the context block for each turn by actively querying the session's
knowledge store.

**Entry point:** `curate_context()` in `curator/pipeline.py` and re-exported from `curator/__init__.py`
- Gated by: `CURATOR_ENABLED`, `FILE_CACHE_ENABLED`, cold-start turn count
- Model/URL/key resolved from `CURATOR_*` settings → fall back to extractor settings
- Hard latency cap: `asyncio.wait_for(loop, timeout=CURATOR_LATENCY_BUDGET_MS/1000)`
- Returns `AssembledContext` on success, `None` on timeout/failure (triggers fallback)

**Two-pass mode** (when `BACKGROUND_PASS_ENABLED=true`):

1. **Background pass** (`run_background_pass()` in `curator/pipeline.py`):
   - Triggered after each upstream response in `_run_extraction()`
   - Runs a full curator loop with up to `BACKGROUND_PASS_MAX_ITERATIONS` (default 12) tool calls
   - Captures a `SessionBriefing` from the result: file contents, outlines, key facts, decisions
   - Caches the briefing in session state with the source turn number
   - Gated by `BACKGROUND_PASS_LATENCY_BUDGET_MS` (default 30s) — `asyncio.wait_for` timeout; on timeout, logs and returns silently
   - Debounced by `BACKGROUND_PASS_DEBOUNCE_MS` (default 2s) — skips if a pass ran too recently

2. **Inline pass** (`_run_with_briefing()` in `curator/pipeline.py`):
   - If a fresh briefing exists (`source_turn >= turn_number - 2`), the curator runs with only 2 iterations
   - The briefing is formatted into the system prompt as pre-fetched context (file contents, outlines, key facts)
   - Falls through to standard full curator run if briefing is missing or stale

3. **Briefing schema** (`curator/briefing.py`):
   - `SessionBriefing`: list of `PreFetchedFile` (path, content, outline), key facts, decisions
   - `format_briefing_for_prompt()`: renders briefing as structured text sections
   - 30K char cap on formatted briefing text

4. **Briefing cache** (`curator/state.py`):
   - `cache_briefing(session_id, briefing, turn)`: stores briefing with turn metadata
   - `get_briefing(session_id)`: retrieves cached briefing
   - `is_briefing_fresh(briefing, current_turn)`: checks staleness threshold

5. **Result fidelity** (`curator/result.py`):
   - `CuratorToolCall.raw_result`: full tool result text (excluded from `to_dict()`)
   - `_build_briefing_from_result()` prefers `raw_result` over `result_preview` for file content and outlines

**Loop:** `_run_curator_native()` in `curator/loop.py`
- Up to `CURATOR_MAX_ITERATIONS` (default 6) tool-call iterations
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

**14 curator tools** in `ALL_CURATOR_TOOLS` (`curator/tools.py` + `curator/schemas.py`):

| Tool | What it returns |
|------|----------------|
| `list_session_files` | Markdown table: path, lines, last-turn |
| `get_file` | Full content (≤200 lines) or 10-line preview + hint to use `get_file_lines` |
| `get_file_outline` | Symbol index (functions/classes with line numbers) for large files — use before `get_file_lines` |
| `get_file_lines` | 1-indexed line slice with line numbers; clamps to EOF |
| `search_facts` | Keyword substring match over active facts, up to 20 results |
| `search_facts_semantic` | Cosine similarity search over fact embeddings; falls back to substring when embeddings unavailable |
| `get_session_goal` | Session goal string |
| `get_recent_decisions` | Numbered list of decisions with turn numbers |
| `get_touched_files` | Path / status / turn table for all files touched in session |
| `get_checkpoint` | Current session checkpoint (summary, next_step, confidence) |
| `get_open_issues` | Active issues (open blockers and errors) |
| `get_last_verification` | Most recent command run + pass/fail/partial status |
| `select_relevant_turns` | Prune the middle-section turn inventory — mark which historical turns to retain |
| `prefetch_file` | Read a file from disk and cache it for the session (with optional focus synopsis) |

**Tool set specialization** (`curator/schemas.py`):
- **PREPPER_TOOLS** (background pass): `ALL_CURATOR_TOOLS` (14) + `score_file_relevance` (prepper-only) = 15 total
  - `score_file_relevance` — ranks all cached files by relevance to a given query; prepper-only optimization for anticipating next-turn requirements
- **ASSEMBLER_TOOLS** (inline assembler): minimal set = `select_relevant_turns` + `get_file_lines` (2 tools)
- **ALL_CURATOR_TOOLS** (full single-bot curator): 14 tools (default)

**Curator support modules:**
- `prompts.py` — system prompts and turn-specific prompt templates for curator invocations
- `result.py` — `CuratorResult` dataclass and result builder; tracks `raw_result` (full text) vs `result_preview`
- `schemas.py` — OpenAI-compatible tool schemas (function definitions + parameters) for ALL/PREPPER/ASSEMBLER tool sets
- `state.py` — session briefing cache (store/retrieve/staleness checks)
- `briefing.py` — `SessionBriefing` model, `PreFetchedFile` items, briefing formatting for prompt injection

**Two-curator mode** (when `curation_mode="two_curator"` — `config.py`):

Decouples briefing generation (background prepper) from context assembly (inline assembler):

1. **Prepper** (`curator/prepper.py`):
   - Runs post-turn in background via `run_prepper()` (spawned from `_run_extraction()`)
   - Receives PREPPER_TOOLS (14 base + `score_file_relevance`)
   - Iterates up to `CURATOR_MAX_ITERATIONS` (default 4, same as full curator)
   - Builds and caches a `SessionBriefing` (file contents, outlines, facts, decisions) with turn metadata
   - Gated by `BACKGROUND_PASS_LATENCY_BUDGET_MS` (default 30s); logs and returns silently on timeout

2. **Assembler** (`curator/assembler.py`):
   - Runs inline on user turns with ASSEMBLER_TOOLS (minimal: `select_relevant_turns`, `get_file_lines`)
   - If a fresh briefing exists, receives it pre-formatted in the system prompt
   - Quick 1–2 iteration runs to just refine turn selection; briefing provides most context
   - Falls back to full single-curator run if briefing is stale or missing

3. **Briefing state management** (`curator/state.py`):
   - `cache_briefing(session_id, briefing, turn)`: store after prepper
   - `get_briefing(session_id)`: retrieve for assembler
   - `is_briefing_fresh(briefing, current_turn)`: staleness check (default threshold: source_turn >= turn_number - 2)

**Event-driven curator worker** (when `CURATOR_WORKER_ENABLED=true` — `curator/worker.py`, Phase 1):

Replaces the request-coupled prepper scheduling. Instead of spawning `run_background_pass()`
from the `_run_extraction()` tail only when `is_user_turn AND not tool_calls` (which starves the
prepper on agentic turns that complete with `finish_reason=stop`/`is_user_turn=False`, and which
`swap_background_task()` cancelled on the next turn), the extraction tail enqueues a `SessionEvent`
onto a long-lived per-session `CuratorWorker` on **every turn boundary**:
   - `WorkerRegistry` owns one `CuratorWorker` per active session; idle-gated (sleeps on an empty
     queue) and evicted after `CURATOR_WORKER_IDLE_TTL_S` of inactivity (pruned in the main.py cleanup loop).
   - The worker debounces a burst of events (`CURATOR_WORKER_DEBOUNCE_MS`) and runs exactly one
     `run_background_pass()` per coalesced burst — **never cancelling an in-flight pass** (the fix vs
     `swap_background_task`). The pass reuses the registered prepper, so curation logic is unchanged.
   - Observability: the `/metrics` `curator_worker_diag` block (`prepper_fires`, `prepper_starved`,
     `prepper_cancels`, `hot_path_llm_calls`, `avg_briefing_lag_turns`) quantifies firing vs starvation.
   - Default off; when off, the legacy `swap_background_task` path is preserved unchanged.

**System prompt** (`curator/prompts.py`):
- Pre-loaded checkpoint in user prompt — skip `get_checkpoint` unless a refresh is needed
- For files >100 lines: call `get_file_outline` first, then `get_file_lines` for relevant range
- Use `search_facts` for keyword lookups; use `search_facts_semantic` when terminology may differ
- 3–6 tool calls per run; hard latency cap via `CURATOR_LATENCY_BUDGET_MS`
- Output format: `=== SESSION GOAL ===`, `=== CURRENT STATE ===`, `=== OPEN ISSUES ===`, `=== LAST VERIFICATION ===`, `=== RELEVANT CODE ===`, `=== KEY FACTS ===`, `=== DECISIONS ===`

### Shared Utilities (`archolith_proxy/shared/`)

- `shared/text_utils.py` is the cross-layer utility home for `_build_outline()`, `_normalize()`, `_tokenize()`, and `jaccard_similarity()`
- This breaks the earlier `curator -> openai.chat` and `graph -> extractor.dedup` dependency leaks

### Filter Startup Gate

When `FILTER_ENABLED=true` (or legacy `RTK_ENABLED=true`), the proxy performs a fail-fast check
at startup: if the `archolith_filter` package is imported but not installed, the proxy raises
`RuntimeError("archolith_filter package not found — cannot honor FILTER_ENABLED=true")` and exits.

This prevents silent fail-open behavior that would mask configuration errors. Operators must either:
- Set `FILTER_ENABLED=false` (default safe mode), or
- Ensure `archolith_filter` is installed and importable in the Python environment

The gate runs in `main.py` during app initialization, before the FastAPI server binds to a port.

### Operator Surfaces

- `GET /admin/config` and `PATCH /admin/config` expose runtime-tunable settings
- `GET /admin/config-delta` shows the persisted override delta relative to base env settings
- Runtime override persistence uses `config_overrides.json` at the project root and reloads on startup via `_load_config_overrides()` + watcher polling
- `PATCH /admin/config?persist=false` applies an override in-memory only (no `config_overrides.json` write) — used by benchmarks so a run does not mutate global config
- Per-session config: an `X-Session-Config` request header on `/v1/chat/completions` scopes config to one session (persisted on the Session node), without touching global config — see the per-session config overlay under Config

### Agent-Solo Compression (`archolith_proxy/proxy/agent_solo.py`)

Mechanical token reduction for agent-solo turns (tool-call continuations where the
last message role is "tool"). These comprise ~85% of requests in typical coding sessions.
No LLM call is involved — all strategies are deterministic and sub-millisecond.

**Entry point:** `compress_agent_solo()` — called from `chat.py` when the request is
classified as an agent-solo turn. Returns `(messages, stats_dict)`.

**Two-phase pipeline:**

1. **Curator prefix cache** — if a cached curator rewrite exists from the most recent
   user turn, splice it into the message prefix. Detection is O(1): compare message
   count + md5 fingerprint of the boundary message (`role:content[:200]`). This solves
   the fundamental persistence problem: the client re-sends the full original history
   on every API call, so curator savings evaporate unless the proxy re-applies the
   cached rewrite on each subsequent agent-solo turn.

2. **RTK Layer 3 strategies** — delegates to `archolith_filter.agent_solo.compress_agent_solo_turn()`
   with four composable strategies (D→C→B→A):
   - **D (Compact)**: Replace large Write/Edit/create_file arguments in completed tool_use
     calls with compact summaries. The model can Read the file to recover. Default on.
   - **C (Filter middle)**: Apply `filter_output()` to compressible tools in older turns.
   - **B (Dedup)**: Replace byte-identical tool results with compact markers via per-session
     `DedupeTracker`.
   - **A (Shrink)**: Cap every tool-role message to `shrink_max_tokens * 4` chars.

**Curator prefix cache internals:**

| Function | Purpose |
|----------|---------|
| `cache_curator_rewrite(session_id, original, rewritten)` | Store cache after successful curator rewrite (called from `chat.py`) |
| `_apply_curator_prefix(session_id, messages)` | Splice cached rewrite into message prefix on agent-solo turns |
| `_fingerprint_message(msg)` | md5 of `role:content[:200]` for O(1) boundary check |
| `clear_curator_cache(session_id)` | Invalidate cache for a session |
| `clear_session_hashes(session_id)` | Clear both dedup trackers and curator cache |

**Stats dict keys:** `chars_saved_curator_cache`, `chars_saved_compact`, `chars_saved_shrink`,
`chars_saved_dedup`, `chars_saved_middle`, `total_chars_saved`, `strategies_applied`,
`skipped_reason`.

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
- Legacy mode: single generic extraction call over assistant text + recent tool results
- Current optional mode: per-tool extraction dispatch via `extract_facts_per_tool()`
- Registry-based routing maps tool names to specialized extractor classes (`bash`, `grep`, `glob`, `ls`, `find`, `web_search`, `web_fetch`, `memory_recall`, fallback)
- LLM-backed extractors are semaphore-limited by `extractor_llm_concurrency`; non-LLM extractors run fully concurrently
- Produces structured facts, decisions, issues, verifications, and checkpoint state
- Runs async (off critical path, concurrent with user think time)

### Session Graph (`archolith_proxy/graph/`)
- Access is brokered through the `GraphBackend` protocol, not direct database calls
- `Neo4jBackend` wraps the legacy graph modules and remains the bare-config default in `Settings`
- `LadybugBackend` is the zero-infra path used by the public bootstrap docs and file-cache-heavy local runs
- Session lifecycle: create, query, invalidate, expire, promote
- Node families: session, fact, file, decision, checkpoint, issue, verification, cached file content, cached file outline

### Config (`archolith_proxy/config.py`)

**Deployment Profiles.** `ARCHOLITH_PROFILE` bundles coherent flag sets:

| Profile | What it enables | Requirements |
|---------|----------------|-------------|
| `passthrough` | Nothing (code default — all flags default-off) | None |
| `mechanical` | Agent-solo compression (shrink/dedup/compress_middle at 3K threshold) + output filtering | `archolith_filter` peer package |
| `curated` | Mechanical + curator LLM + background pass + file cache | `archolith_filter` + graph backend + curator API key |
| `full` | Curated + embeddings + per-tool extraction + recall tool | All of the above + embedding API key |

Precedence: `session > config_overrides.json > env > profile > default`. Explicit env vars always win over profile values.

- Upstream API URL and credentials (validated: must be http/https)
- Graph backend selection (`graph_backend`) plus backend-specific settings
- Extraction model selection
- Token budgets, TTL, coherence tail size
- Cold start turns gate (user-turn count is authoritative; token threshold is retained as a compatibility setting)
- Retry settings: upstream and Neo4j (max retries, backoff base seconds)
- Promotion settings (if wired to long-term memory)
- Memory engine config (JSON array of engine definitions)
- Promotion policy defaults (min confidence, dry-run mode)
- Synthetic tools: enabled, circuit breaker thresholds, file recall limits
- Native read interception toggle (`native_read_intercept_enabled`) for serving repeated file reads from cache
- Per-tool extraction toggles (`per_tool_extraction_enabled`, `extractor_llm_concurrency`)
- Agent-solo compression toggles and payload dump switch
- Session token budget: max input tokens per session, budget action (passthrough/reject)
- Settings singleton caching (get_settings / reset_settings)

**Per-session config overlay.** Config can be scoped to a single session without
mutating the global singleton. `get_settings()` checks a `contextvars` overlay
(`_session_settings_ctx`): when set it returns the session's effective settings,
else the process-global singleton (default — behavior-identical). `build_effective_settings(overrides)`
copies the global base and layers the (filtered) session overrides on top, so
precedence is **session > `config_overrides.json` > env > default**. A client sets
overrides via an `X-Session-Config` request header (JSON object) on `/v1/chat/completions`:
the proxy merges it into the session's persisted overrides (`Session.config_overrides`,
see data_models.md), activates the overlay for the request, and resets it after the
response via a request-scoped dependency. Async follow-up work (extraction, the
curator background pass spawned with `asyncio.create_task`) inherits the overlay
through context copying. Because that endpoint is unauthenticated, `SESSION_CONFIG_DENYLIST`
blocks per-session override of secrets/infra (upstream/extractor/curator/embedding/memory
URLs + keys, `admin_token`, `ladybug_db_path`); denied and unknown fields are rejected
loudly (logged) and never persisted. Set/get/reset helpers: `set_session_settings`,
`reset_session_settings`, graph `set_session_config_overrides` / `get_session_config_overrides`.

**Token accounting (`archolith_proxy/token_accounting/`).** The assembly gate keys
on a structural token estimate, not the crude `len(json.dumps(m))//4`. The crude
estimate sees message content only and is blind to the top-level `tools` array, so
the gate decided on a large underestimate (e.g. 10 vs ~17,900 tokens on a 20-tool
request). `build_telemetry` computes content / structural (content + tool schemas +
`tool_calls` + tool-result payloads + framing) / client-reported estimates and a
gate decision; the gate uses `gate_input_tokens = max(structural, client_reported)`.
A client may supply `X-Context-Token-Hint`. The estimator uses tiktoken (cl100k_base)
when present and falls back to `len/3.6`. It runs on the request hot path via
`asyncio.to_thread` — tiktoken releases the GIL, so encoding does not block the
event loop. The per-turn trace records the breakdown plus the actual upstream
`prompt_tokens` (`prompt_tokens_actual`) so estimate-vs-actual is inspectable. The
session token budget still uses the legacy content estimate (controlled migration).

**Important default nuance:** the Python `Settings` class still defaults `graph_backend` to `neo4j`
and `upstream_base_url` to DeepSeek. The repo README and `.env.example` are optimized around the
public/local bootstrap path (`ladybug` + OpenAI-compatible upstream). Keep both realities explicit
when updating docs or helping operators.

### Trace Observability (`archolith_proxy/trace/`)

Per-turn observability records and session-level telemetry.

**Core modules:**
- `builder.py` — `TraceBuilder` incremental constructor: populate TurnTrace fields across request/assembly/response/extraction phases
- `store.py` — `TraceStore` persistence (local JSONL or HTTP POST endpoint); retention policy (trace_retention_days, default 30)
- `router.py` — trace initialization middleware; builder injection into request context

**Trace records:**
- `TurnTrace` (67 fields) — canonical single-turn record with: request metadata, assembly mode/latency, compression stats, curator decisions, filter metrics, recall usage, extraction results
- `BackgroundPassTrace` — separate record for background curator passes (timing, outcome, tool usage, briefing cached)
- `SessionTraceSummary` — aggregated view of session: turns, tokens, savings ratios, mode distribution, facts stored, harness env, proxy config snapshot

**File format (JSONL):** one record per line, session_id in filename (`<session_id>.jsonl`). Session-id sanitization converts non-hex/UUID chars to underscores for safe filenames. Trace records are accumulated in-memory, flushed to disk periodically or on session expiry.

### Memory Engine & Promotion (`archolith_proxy/memory/`)
- **Registry** (`registry.py`): Config-driven engine registration, lazy adapter instantiation, priority-based default resolution
- **Canonical models** (`models.py`): `PromotionRecord`, `PromotionResult`, `EngineCapabilities`, `MemoryEngineConfig`
- **Adapter base** (`adapters/base.py`): Abstract contract — validate_config, capabilities, healthcheck, promote_fact, optional batch/dedupe/CRUD
- **Concrete adapters** (`adapters/`): 9 total — `archolith_memory`, `basic_memory`, `claude_mem`, `cognee`, `generic_http`, `mem0`, `nocturne_memory`, `openmemory`, `zep`
- **Promotion service** (`promotion.py`): Policy layer (confidence threshold, fact type allowlist, multi-turn survival), dedupe, dry-run, audit trail

### Synthetic Session-Summary Tools (`archolith_proxy/proxy/synthetic_tools.py`)

> **DEPRECATED — Removal planned.** Synthetic tools caused the 2026-05-25 production meltdown and are documented as "direction change — not pursued" in the ROADMAP. The entire synthetic-tools subsystem (module, injection wiring, circuit-breaker counters, forced-non-streaming SSE conversion, config flags, metrics, docs) is targeted for deletion in the [Dead Code Removal plan](plans/archolith-context-dead-code-removal-plan.md). This section is retained until that plan executes, after which it should be removed in full.

Agent-initiated tools that the proxy injects into every request when a session is active
and `SYNTHETIC_TOOLS_ENABLED=true`. The model can call these to get structured summaries
of session work and files accessed without the harness needing to support custom tools.

**Three synthetic tools:**

| Tool | What it returns |
|------|----------------|
| `recall_session_work` | Structured summary of all work done this session |
| `recall_files_read` | List of files accessed, to skip redundant re-reads |
| `recall_file` | Content of a specific file (line-limited, from proxy cache) |

**How it works:**
1. `inject_synthetic_tools(body)` — add tool definitions before forwarding upstream
2. Upstream responds with tool_calls containing a synthetic name
3. `handle_non_streaming_synthetic()` detects the call, generates the result, re-sends
4. `strip_synthetic_tools` / `strip_synthetic_from_response` clean up so client never sees internal tooling
5. On re-send failure: `_fallback_strip_synthetic()` strips synthetic calls and normalizes `finish_reason`

**Non-streaming path only** (same limitation as `__archolith_recall`).
When the original client requested streaming, the forced-non-streaming path converts
the result to SSE via `_wrap_response_as_sse()`.

**Critical bug fixed (2026-05-25):** `_wrap_response_as_sse()` previously only emitted
`role`, `content`, and `finish_reason` deltas — never `tool_calls`. When the model
made mixed calls (synthetic + real), OpenCode received `finish_reason: "tool_calls"`
but no tool call data, causing an infinite retry loop. Fixed by emitting tool_calls as
separate name+argument deltas with proper `index` keys (matching the OpenAI streaming spec).

### Native Read Intercept (`archolith_proxy/proxy/tool_injection.py`)

Transparent cache-backed read interception for repeated file reads inside the same session.

- When `native_read_intercept_enabled=true` and the synthetic tooling path is active, the proxy can answer repeated file reads from `FileContent` instead of forwarding the read upstream
- Works only for files already cached earlier in the session
- Emits cache-hit / cache-miss metrics (`native_read_cache_hits`, `native_read_cache_misses`, `file_cache_invalidations`)
- Written files invalidate stale cache entries before new content is re-cached during extraction

### Circuit Breaker (`archolith_proxy/proxy/circuit_breaker.py`)

Per-session circuit breaker that prevents runaway synthetic tool re-injection loops.
State is in-memory only (resets on proxy restart).

**Thresholds (configurable via env):**
- `SYNTHETIC_CIRCUIT_MAX_CONSECUTIVE` (default 3): consecutive failures before opening circuit
- `SYNTHETIC_CIRCUIT_COOLDOWN_S` (default 300): seconds to keep circuit open
- `SYNTHETIC_CIRCUIT_MAX_TOTAL` (default 10): total failures before session-lifetime hard-disable

**Flow:**
1. Before calling `inject_synthetic_tools()`, `chat.py` checks `is_synthetic_allowed(session_id)`
2. If circuit is open → skip injection, increment `synthetic_injections_skipped` metric
3. On success → `record_synthetic_success()` resets consecutive counter
4. On failure (exception or fallback) → `record_synthetic_failure()` increments counters
5. After 3 consecutive → circuit opens for 5 min; after 10 total → hard-disable for session lifetime

**Also tracks per-session token budget:** `add_session_tokens()` / `is_session_over_budget()`
for the `MAX_INPUT_TOKENS_PER_SESSION` hard cap.

### Dashboard (`archolith_proxy/static/`)

- `dashboard.html` — interactive trace viewer and session inspector
  - Real-time turn timeline with turn metrics (input/output tokens, savings ratio, assembly mode)
  - Per-turn drill-down: original messages, rewritten messages, facts selected, curator tool log
  - Background pass timeline (parallel lane showing prepper latency, briefing size)
  - Session-level aggregates: total tokens, cumulative savings, mode distribution
  - Filter availability indicator and strategy breakdown

## Isolation from Long-term Memory

| | Long-term memory (archolith-memory) | Session context (this project) |
|--|-------------------------------------|-------------------------------|
| Storage backend | Library-defined (PostgreSQL, SQLite, or custom adapter) | LadybugDB (default, embedded) or Neo4j |
| Isolation | Separate store entirely — no shared tables or labels | Session-scoped; all nodes carry `session_id` |
| Lifecycle | Persistent, cross-session, survives proxy restarts | Ephemeral, TTL per session (default 24h) |
| Write path | Proxy promotion pipeline (high-confidence facts) + agent via MCP `add_memory` | Proxy extracts automatically every turn |
| Read path | Proxy injects via `archolith_memory.recall()` (planned); MCP `recall_memories` / `build_context` today | Proxy assembler (internal, no agent tool call needed) |

When running without archolith-memory, long-term memory is handled by whatever the agent's MCP server provides (`cth.mcp.memory` or equivalent). The proxy promotion pipeline writes to whichever memory backend is configured via `MEMORY_ENGINES_JSON`.

**Neo4j isolation note (when used as session backend):** Session nodes carry the `:ContextSession` label; any long-term memory nodes in the same Neo4j instance use `:Memory`. All queries are label-scoped. Session data is bulk-droppable: `MATCH (n:ContextSession) DETACH DELETE n`.

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
# Code default: neo4j. Public/local bootstrap docs usually switch this to ladybug.
GRAPH_BACKEND=neo4j
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
CURATOR_MAX_ITERATIONS=6
CURATOR_LATENCY_BUDGET_MS=6000 # hard timeout; falls back to heuristic on expiry

# Two-pass curator (background pre-fetch + inline briefing)
BACKGROUND_PASS_ENABLED=false # enable two-pass curator mode
BACKGROUND_PASS_MAX_ITERATIONS=12 # tool call budget for background pass
BACKGROUND_PASS_DEBOUNCE_MS=2000 # minimum ms between background passes
BACKGROUND_PASS_LATENCY_BUDGET_MS=30000 # hard timeout for background pass

# Retry / resilience
UPSTREAM_MAX_RETRIES=3
UPSTREAM_RETRY_BACKOFF_BASE_S=0.5
NEO4J_MAX_RETRIES=3
NEO4J_RETRY_BACKOFF_BASE_S=1.0

# Synthetic session-summary tools
SYNTHETIC_TOOLS_ENABLED=false         # inject recall_session_work, recall_files_read, recall_file
SYNTHETIC_CIRCUIT_MAX_CONSECUTIVE=3   # consecutive failures before circuit opens
SYNTHETIC_CIRCUIT_COOLDOWN_S=300      # cooldown duration in seconds
SYNTHETIC_CIRCUIT_MAX_TOTAL=10        # total failures before session-lifetime disable
RECALL_FILE_MAX_LINES=200             # max lines returned per recall_file call
RECALL_FILE_MAX_BYTES=24000           # secondary byte cap
RECALL_FILE_CONTEXT_LINES=3           # padding lines around a symbol

# Session token budget
MAX_INPUT_TOKENS_PER_SESSION=2000000  # 0 = unlimited; stop context management when exceeded
SESSION_TOKEN_BUDGET_ACTION=passthrough  # "passthrough" (forward raw) or "reject"

# Agent-solo turn compression (mechanical, no LLM)
AGENT_SOLO_SHRINK_ENABLED=false          # A: char-budget all tool results
AGENT_SOLO_DEDUP_ENABLED=false           # B: cross-turn content hash dedup
AGENT_SOLO_COMPRESS_MIDDLE_ENABLED=false # C: filter compressible tools in middle
# Strategy D (compact Write/Edit args) is always on when RTK is installed
AGENT_SOLO_SHRINK_MAX_TOKENS=2000        # per-result token cap for strategy A
AGENT_SOLO_MIN_INPUT_TOKENS=8000         # skip compression below this input size
AGENT_SOLO_DUMP_PAYLOADS=false           # dump payloads to data/agent_solo_payloads/

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
| `GET /live` | Liveness probe — process is up |
| `GET /ready` | Readiness probe — graph backend and upstream reachability |
| `GET /health` | Health check: Neo4j status, upstream status, version, uptime |
| `GET /metrics` | Process-level counters: total_requests, assembly_modes, extraction_successes/empties/failures, upstream_errors, neo4j_errors, active_sessions, token_savings_estimated, total_input_tokens_seen, trace_records, trace_sessions, uptime, curator_calls, curator_timeouts, curator_fallbacks, synthetic_tool_successes, synthetic_tool_failures, synthetic_circuit_opens, synthetic_circuit_hard_disables, synthetic_injections_skipped, synthetic_circuit_states (per-session) |
| `GET/PATCH/POST /admin/config` | Runtime-tunable config surface for experiments and operator control |
| `POST /admin/shutdown` | Graceful SIGTERM-based shutdown path |
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
| `GET/POST/DELETE /trace/benchmark/session-id` | Benchmark session-id override for traceable scripted runs |
| `GET /memory-engines` | List configured memory engines with health status |
| `GET /memory-engines/{id}` | Single engine details, health, and capabilities |
| `GET /promotions` | Promotion history and stats |
| `POST /promotions/retry/{id}` | Retry a failed promotion |
| `GET /dashboard/` | Web dashboard (single-page HTML, zero build step) with per-turn RTK strategy savings, proxy-recall annotations, and curator proxy-note visibility |
| `GET /ws/stream` | WebSocket live event stream |

Metrics are in-memory (`_metrics` dict surfaced via `archolith_proxy/metrics.py`), reset on process restart. Prometheus-compatible OpenMetrics format is a future goal.

`assembly_modes` tracks: `graph`, `fallback`, `cold_start`, `passthrough`, `curator`, `briefing`, `briefing_stale`, `agent_solo`, `agent_solo_compressed`.

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

## Token Reduction — archolith-filter Integration

Token reduction is handled by `archolith-filter`, a standalone Python library that lives
in a sibling project (`projects/archolith/archolith-filter`).  It is the **preferred and
canonical** token reduction toolkit for this workspace.  archolith-context treats it as
a first-class peer: when installed, it is used deeply at every pipeline point where
token reduction matters; when absent, all RTK paths are fail-open and the proxy operates
without RTK passes.

### Layers

| Layer | Module | What it does |
|-------|--------|-------------|
| Layer 1 — Output Filtering | `archolith_filter.filter_output` | Strips noise/boilerplate from tool results: git diffs, test output, build logs, lint, directory trees, JSON payloads, search results. 13 named categories + cross-turn deduplication via `DedupeTracker`. ANSI stripping is always applied. Fail-open: exceptions return ANSI-stripped input unchanged. |
| Layer 2 — Shrink | `archolith_filter.shrink` | Deterministic token budgeting: `shrink_oversized_tool_call_args_by_tokens` collapses large string values in assistant tool_call JSON (Write/Edit file content); `shrink_oversized_tool_results_by_tokens` truncates tool-role messages over a per-message token cap. |
| Layer 3 — Agent-Solo | `archolith_filter.agent_solo` | Four composable strategies (D→C→B→A) for tool-call continuation turns. Strategy D compacts completed Write/Edit args, C filters middle-section tools, B deduplicates byte-identical results, A char-budgets all results. Called by `archolith_proxy/proxy/agent_solo.py`. |

### Adapter (`archolith_proxy/rtk.py`)

A thin adapter that lazy-loads archolith-filter with independent per-function sentinels
(sentinel = `False` → unresolved, `None` → unavailable, callable → loaded).  Each
wrapper is **fail-open**: if archolith-filter is not installed, `ImportError` sets the
sentinel to `None` and the wrapper returns its input unchanged.

**Public API exposed by the adapter:**

| Function | RTK call | Where used |
|----------|----------|-----------|
| `filter_tool_messages(messages, enabled)` | Layer 1 `filter_output` per tool-role message | `filter_request_body()` — applied to every outbound request |
| `filter_single_tool_result(content, tool_name)` | Layer 1 `filter_output` on one string | `_collect_recent_tool_results()` in `chat.py` — denoises tool output before extractor LLM sees it |
| `shrink_tool_call_args(messages, max_tokens, enabled)` | Layer 2 `shrink_oversized_tool_call_args_by_tokens` | `filter_request_body()` — collapses large Write/Edit args in assistant history |
| `shrink_tail_tool_results(messages, max_tokens_per_result)` | Layer 2 `shrink_oversized_tool_results_by_tokens` | `rewrite_messages()` in `rewrite.py` — caps each tool-role message in the coherence tail |

### Integration Points

```
REQUEST PATH:
  filter_request_body()
    └── filter_tool_messages()          ← Layer 1: strip noise from tool-role history
    └── shrink_tool_call_args()         ← Layer 2: collapse Write/Edit file content args

EXTRACTION (async, off critical path):
  _collect_recent_tool_results()
    └── filter_single_tool_result()     ← Layer 1: denoise before extractor LLM budget

CONTEXT ASSEMBLY:
  rewrite_messages() — tail append
    └── shrink_tail_tool_results()      ← Layer 2: cap token footprint of each tail tool msg
```

### Relationship Between Projects

archolith-filter is the first concrete module in the [Archolith Ecosystem](#archolith-ecosystem) —
the same optional-peer pattern applies to archolith-memory (planned) and any future modules.

archolith-filter is **not a dependency** of archolith-context in the `pyproject.toml` sense — it is an optional peer.  This preserves the ability to run archolith-context standalone without the RTK library installed.  When both are present in the same virtualenv, RTK is used automatically with no configuration required.

```bash
uv pip install -e ../archolith-filter  # from inside archolith-context
```

archolith-filter has zero dependency on archolith-context and can be used independently as a standalone token-reduction library in any Python project.

## Plugin System

archolith-proxy ships with a formal plugin contract so sibling modules (filter, audit, memory) integrate through a single standard lifecycle instead of ad-hoc sentinel patterns.

### ProxyPlugin Protocol (`archolith_proxy/plugins/registry.py`)

A `@runtime_checkable` Protocol. Any object that implements these six members satisfies the contract:

| Member | Purpose |
|--------|---------|
| `plugin_id: str` | Unique identifier (`"filter"`, `"audit"`, `"memory"`) |
| `plugin_version: str` | Semantic version of the plugin |
| `async activate() -> bool` | Called at proxy startup. Return True if ready, False if degraded. Raise to report error. Proxy always starts. |
| `async deactivate() -> None` | Called at proxy shutdown. Best-effort; exceptions are swallowed. |
| `async healthcheck() -> dict` | Returns `{"status": "ok"|"degraded"|"unavailable", ...}` |
| `contribute_metrics() -> dict[str, int|float]` | Flat counters for `/metrics`. Must not block. |

### PluginRegistry (`archolith_proxy/plugins/__init__.py`)

Process-level singleton (`get_plugin_registry()`). Responsibilities:

- `register(plugin)` — called by built-in plugins at startup
- `activate_all()` — called in lifespan startup; fail-safe per plugin
- `deactivate_all()` — called in lifespan shutdown
- `aggregate_metrics()` — merges plugin metrics under `plugins.<id>.*` keys
- `healthcheck(id)` — delegates to plugin's `healthcheck()`
- `list_plugins()` — id, version, status for admin surface

### Built-in Plugins

| Plugin | File | What it wraps |
|--------|------|---------------|
| `FilterPlugin` | `plugins/filter_plugin.py` | `filter_adapter.py` sentinels; reads `FilterTelemetryStore` |
| `MemoryPlugin` | `plugins/memory_plugin.py` | `memory/registry.py`; reports engine count + promotion counters |
| `AuditPlugin` | `plugins/audit_plugin.py` | `archolith_mcp_audit`; optional `LiveAccumulator` attachment |

### Configuration

```env
PLUGINS_ENABLED=filter,memory   # Only these activate (empty = all)
PLUGINS_DISABLED=audit          # Always blocked even if in ENABLED
```

Version compatibility is enforced: `MIN_PLUGIN_VERSIONS` in `registry.py` defines the minimum acceptable version per plugin. A mismatch logs an error with a `pip install archolith-proxy[<id>]` hint; the proxy still starts.

### Admin Surface

- `GET /plugins` — list all plugins with status, version, counts
- `GET /plugins/{id}` — single plugin detail + live health + metrics
- `GET /metrics` → `plugins` key — aggregated plugin metrics grouped by plugin ID

## External Dependencies

| Service | Purpose | Required |
|---------|---------|----------|
| LadybugDB | Session graph + file cache (default backend — embedded, zero infra) | Default — no infra needed |
| Neo4j | Session graph alternative for production deployments | Optional — only when `GRAPH_BACKEND=neo4j` |
| OpenAI API (gpt-4.1-mini) | Fact extraction + curator LLM | Optional — extraction skipped on failure |
| OpenAI API (embeddings) | Semantic similarity for `search_facts_semantic` | Optional — falls back to substring search |
| Upstream LLM API | Target for proxied requests | **Yes — required** |
| archolith-filter | Token reduction (Layer 1 + Layer 2) | Optional peer (fail-open) |
| archolith-memory | Long-term cross-session memory | Optional peer (planned — fail-open) |
| Memory backend API (e.g. cth.mcp.memory) | Promotion target for durable facts | Optional — only when `PROMOTION_ENABLED=true` |

## Port Assignment

| Service | Port |
|---------|------|
| Context Engine Proxy | 9800 |
| Neo4j (shared instance, label-isolated) | 7687 |
