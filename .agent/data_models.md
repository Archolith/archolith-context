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
@dataclass
class AssembledContext:
    system_message: dict  # original system prompt (pass-through)
    graph_context: list[dict]  # synthesized messages from graph facts
    coherence_tail: list[dict]  # last N raw messages (verbatim)
    token_estimate: int  # estimated total tokens
    facts_retrieved: int  # how many facts contributed
    session_id: str
```

### ExtractionResult
What the extractor produces after parsing a response.

```python
@dataclass
class ExtractionResult:
    facts: list[Fact]
    files_touched: list[str]
    decisions: list[Decision]
    invalidated_fact_ids: list[str]  # facts superseded by this turn
    turn_number: int
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
`_metrics` dict in `src/main.py` — exposed via `GET /metrics`:

| Key | Type | Description |
|-----|------|-------------|
| total_requests | int | All HTTP requests processed |
| assembly_modes | dict[str, int] | Count per assembly_mode |
| extraction_successes | int | Successful fact extractions |
| extraction_failures | int | Failed fact extractions |
| upstream_errors | int | Upstream API errors (5xx, timeout, connection) |
| neo4j_errors | int | Neo4j query failures |
| active_sessions | int | Sessions currently in graph |
| token_savings_estimated | int | Estimated tokens saved by context assembly |
| total_input_tokens_seen | int | Total input tokens across all requests |
| uptime_s | float | Seconds since process start |

### ExtractionResult
What the extractor produces after parsing a response.

```python
@dataclass
class ExtractionResult:
    facts: list[Fact]
    files_touched: list[str]
    decisions: list[Decision]
    invalidated_fact_ids: list[str]  # facts superseded by this turn
    turn_number: int
```

## Repository / Storage

| Store | Technology | Access Pattern |
|-------|-----------|----------------|
| Session graph | Neo4j (default `neo4j` database, `:ContextSession` label) | Cypher queries via neo4j-driver, label-scoped |
| Embeddings | Stored as node properties in Neo4j | Vector index for similarity search |
| Session metadata | Neo4j Session nodes (`:ContextSession`) | Direct lookup by session_id/fingerprint |
| Cleanup | Neo4j TTL-based sweep | Background task deletes expired sessions by label |
