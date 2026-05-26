# cth.context-engine — Data Models

## Graph Nodes

> **Isolation note:** All session graph nodes carry the `:ContextSession` label. Long-term memory nodes (cth.mcp.memory) carry the `:Memory` label. All queries are label-scoped to prevent cross-contamination.

### Session
Represents a single coding agent conversation.

| Property | Type | Description |
|----------|------|-------------|
| session_id | str | Unique ID (generated on first request) |
| fingerprint | str | Hash of system prompt + first user message |
| goal | str | Extracted session goal/intent |
| created_at | datetime | When session started |
| last_active | datetime | Last request timestamp |
| ttl_hours | int | Hours until expiry |
| status | SessionStatus | active, expired, promoted |

### Fact
An extracted piece of session knowledge.

| Property | Type | Description |
|----------|------|-------------|
| fact_id | str | Unique ID |
| session_id | str | Owning session |
| content | str | The fact text |
| fact_type | FactType | Type classification |
| valid_from | datetime | When fact became true |
| valid_until | datetime? | When superseded (null = still active) |
| invalidated_at | datetime? | When fact was explicitly invalidated (null = still active) |
| confidence | float | Extraction confidence (0–1) |
| source_turn | int | Which turn produced this fact |
| embedding | list[float] | Vector embedding for similarity search |

> **Indexes:** `invalidated_at` and `session_id` on Fact nodes (added Phase 4 for invalidation queries + session-scoped lookups).

### File
A file referenced or modified during the session.

| Property | Type | Description |
|----------|------|-------------|
| path | str | Absolute or project-relative path |
| session_id | str | Owning session |
| last_read_turn | int? | Last turn where file was read |
| last_modified_turn | int? | Last turn where file was edited |
| status | FileStatus | read, modified, created, deleted |

> **Index:** `session_id` on File nodes (added Phase 4 for session-scoped file lookups).

### Decision
An explicit choice made during the session.

| Property | Type | Description |
|----------|------|-------------|
| decision_id | str | Unique ID |
| session_id | str | Owning session |
| summary | str | What was decided |
| rationale | str? | Why (if extractable) |
| turn | int | When decided |
| superseded_by | str? | Later decision that replaced this |

### FileContent
Cached file content stored when the agent reads or writes a file. Enables the curator to serve `get_file` and `get_file_lines` without re-reading from disk.

| Property | Type | Description |
|----------|------|-------------|
| file_id | str | Unique ID (`sha256` of session_id + path) |
| session_id | str | Owning session |
| path | str | Absolute or project-relative path |
| content | str | Full file text |
| sha256 | str | SHA-256 hash of content |
| line_count | int | Number of lines |
| last_updated_turn | int | Turn number of last cache write |
| created_at | datetime | First ingested |

> **Ingest triggers:** Write/create_file tool call args (direct content capture) and Read tool results (content from proxy intercept or result pass-through).

### FileOutline
Symbol index for a cached file. Built on ingest via AST (Python) or regex fallback (other types). Allows the curator to locate functions/classes before fetching full content.

| Property | Type | Description |
|----------|------|-------------|
| outline_id | str | Unique ID |
| session_id | str | Owning session |
| path | str | Path matching the `FileContent` entry |
| outline | str | Newline-delimited symbol list with line numbers |
| last_updated_turn | int | Turn number of last outline write |

> **Format:** Each line: `<type> <name> L<start>[-<end>]` e.g. `def authenticate L42-58`, `class AuthService L10-140`. Cap ~100 symbols.

### Checkpoint
Single-record work state for the session. Overwritten on every extraction turn. Used by the curator's `get_checkpoint` tool and pre-injected into the curator prompt to save one tool-call iteration.

| Property | Type | Description |
|----------|------|-------------|
| session_id | str | Primary key — one record per session |
| summary | str | One sentence: what state work is in right now |
| next_step | str? | Most important next action, or null |
| confidence | float | Extractor confidence in this summary (0–1) |
| source_turn | int | Turn number that produced this checkpoint |
| updated_at | datetime | Last write timestamp |

### Issue
An open or resolved blocker/error discovered during the session.

| Property | Type | Description |
|----------|------|-------------|
| issue_id | str | Unique ID |
| session_id | str | Owning session |
| status | str | `open` or `resolved` |
| summary | str | Description of the problem |
| related_file | str? | File path if directly relevant |
| related_command | str? | Command that surfaced the issue |
| resolution_ref | str? | Fact ID or description of the fix (if resolved) |
| source_turn | int | Turn where issue was first detected |
| resolved_turn | int? | Turn where issue was resolved (null if still open) |
| created_at | datetime | When first recorded |

### Verification
A test run, build, or API call with an observable outcome — recorded when the agent executes a command with known pass/fail semantics.

| Property | Type | Description |
|----------|------|-------------|
| verification_id | str | Unique ID |
| session_id | str | Owning session |
| command | str | Exact command that was run |
| status | str | `pass`, `fail`, or `partial` |
| summary | str | What was tested and the key outcome |
| source_turn | int | Turn where verification ran |
| created_at | datetime | When recorded |

## Graph Edges

| Edge Type | From → To | Meaning |
|-----------|-----------|---------|
| TOUCHES | Session → File | Session interacted with file |
| MODIFIES | Fact → File | Fact describes a file modification |
| IMPORTS | File → File | Import/dependency relationship |
| CAUSED_BY | Fact → Fact | Causal chain (error → fix) |
| SUPERSEDES | Fact → Fact | Newer fact invalidates older |
| SUPPORTS | Fact → Decision | Fact informed a decision |
| BELONGS_TO | * → Session | Ownership edge |

## Enums

### FactType
```python
class FactType(str, Enum):
    FILE_STATE = "file_state"       # "src/app.ts now exports X"
    ERROR = "error"                 # "build fails with TypeError"
    TOOL_RESULT = "tool_result"     # condensed tool output
    DECISION = "decision"           # "chose approach X"
    STATE = "state"                 # "tests passing", "migration applied"
    GOAL = "goal"                   # session objective
    OBSERVATION = "observation"     # general finding
```

### SessionStatus
```python
class SessionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    PROMOTED = "promoted"           # facts moved to long-term memory
```

### FileStatus
```python
class FileStatus(str, Enum):
    READ = "read"
    MODIFIED = "modified"
    CREATED = "created"
    DELETED = "deleted"
```

## DTOs

### AssembledContext
What the assembler produces for the proxy to forward.

```python
class AssembledContext(BaseModel):
    system_message: dict  # original system prompt (pass-through)
    graph_context: list[dict]  # synthesized messages from graph facts
    coherence_tail: list[dict]  # last N raw messages (verbatim)
    token_estimate: int  # estimated total tokens
    facts_retrieved: int  # how many facts contributed
    session_id: str
    files_selected: list[dict]  # files injected into the assembled context
    decisions_selected: list[dict]  # decisions injected into the assembled context
```

### ExtractionResult
What the extractor produces after parsing a response.

```python
class ExtractionResult(BaseModel):
    facts: list[dict]               # [{content, fact_type, confidence}]
    files_touched: list[str]        # file paths
    decisions: list[dict]           # [{summary, rationale}]
    invalidated_fact_ids: list[str] # description strings matched via Jaccard similarity
    turn_number: int
    session_goal: str | None        # inferred or updated goal
    checkpoint: dict | None         # {summary, next_step, confidence}
    issues: list[dict]              # [{summary, status, related_file, related_command}]
    verifications: list[dict]       # [{command, status, summary}]
```

### AssemblyMode
How the chat handler resolved context for a request.

| Mode | Meaning |
|------|---------|
| `cold_start` | Below turn/token threshold — full passthrough |
| `graph` | Graph query succeeded — context assembled |
| `fallback` | Graph query failed — passthrough after attempted assembly |
| `passthrough` | Neo4j not configured — always passthrough |

### Metrics (in-memory, process-level)
`_metrics` dict in `archolith_proxy/metrics.py` — exposed via `GET /metrics`:

| Key | Type | Description |
|-----|------|-------------|
| total_requests | int | All HTTP requests processed |
| assembly_modes | dict[str, int] | Count per assembly_mode |
| extraction_successes | int | Turns where extraction produced and stored one or more facts |
| extraction_empties | int | Turns where extraction ran successfully but produced zero facts |
| extraction_failures | int | Extraction task failures or unavailable extractor responses |
| upstream_errors | int | Upstream API errors (5xx, timeout, connection) |
| neo4j_errors | int | Neo4j query failures |
| active_sessions | int | Sessions currently in graph |
| token_savings_estimated | int | Estimated tokens saved by context assembly |
| total_input_tokens_seen | int | Total input tokens across all requests |
| uptime_s | float | Seconds since process start |

## Repository / Storage

| Store | Technology | Access Pattern |
|-------|-----------|----------------|
| Session graph | Neo4j (default `neo4j` database, `:ContextSession` label) | Cypher queries via neo4j-driver, label-scoped |
| Embeddings | Stored as node properties in Neo4j | Vector index for similarity search |
| Session metadata | Neo4j Session nodes (`:ContextSession`) | Direct lookup by session_id/fingerprint |
| Cleanup | Neo4j TTL-based sweep | Background task deletes expired sessions by label |

## Promotion Models (`src/memory/models.py`)

### PromotionRecord
Canonical payload for outbound fact promotion — one durable fact being promoted to a memory engine.

| Property | Type | Description |
|----------|------|-------------|
| promotion_id | str | Unique ID (auto-generated hex[:16]) |
| session_id | str | Source session |
| source_turn | int | Which turn produced this fact |
| fact_type | str | Type classification (matches FactType values) |
| content | str | The fact text |
| confidence | float | Extraction confidence (0–1) |
| session_goal | str \| None | Session objective at time of promotion |
| touched_files | list[str] | Files referenced by this fact |
| decision_context | str \| None | Rationale if fact_type == decision |
| promotion_reason | str | Why this fact was promoted |
| promoted_at | float | Unix timestamp |
| tags | list[str] | Classification tags |
| dedupe_key | str | Deterministic hash for idempotency |
| source_trace_ref | str \| None | TurnTrace.turn_id for audit trail |

### PromotionResult
Outcome of a single promotion attempt through an adapter.

| Property | Type | Description |
|----------|------|-------------|
| promotion_id | str | Matches the PromotionRecord |
| engine_id | str | Target engine |
| outcome | PromotionOutcome | pending / success / failed / skipped / retry |
| remote_id | str \| None | ID in the target memory system |
| error_message | str \| None | Error details on failure |
| elapsed_ms | float | Latency of the promotion attempt |

### MemoryEngineConfig
Configuration for a single registered memory engine.

| Property | Type | Description |
|----------|------|-------------|
| id | str | Unique engine identifier |
| type | str | Adapter type: cth_mcp_memory, mem0, zep, generic_http |
| enabled | bool | Whether the engine is active |
| priority | int | Higher = preferred default |
| base_url | str | Backend endpoint URL |
| api_key_env | str | Env var name holding the API key |
| extra | dict | Adapter-specific configuration |

### EngineCapabilities
What a memory engine adapter supports.

| Property | Type | Default |
|----------|------|---------|
| promote_fact | bool | True |
| promote_batch | bool | True |
| dedupe_lookup | bool | False |
| list_by_source | bool | False |
| update_promoted | bool | False |
| delete_promoted | bool | False |
| healthcheck | bool | True |
