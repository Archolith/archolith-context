# archolith-context — Data Models

`archolith-context` is the product/repo name. The live Python import surface is
`archolith_proxy`, and older `cth.context-engine` naming may still appear in
historical notes.

The project stores session-state through a backend abstraction:

- `GraphBackend` protocol defines the write/read contract
- `Neo4jBackend` wraps the legacy graph modules
- `LadybugBackend` stores the same logical entities in an embedded local database
- `TraceStore` holds observability records separately from the graph backend

## Core Graph Models

These are the shared Pydantic node shapes in
`archolith_proxy/models/graph_nodes.py`. Backends may store extra timestamps or
derived fields, but these are the canonical application-level fields.

### SessionNode

| Property | Type | Description |
|----------|------|-------------|
| `session_id` | `str` | Stable session identifier |
| `fingerprint` | `str \| None` | Fallback hash when the caller does not provide `X-Session-ID` |
| `goal` | `str \| None` | Current session objective |
| `created_at` | `datetime` | Session creation time |
| `last_active` | `datetime` | Last touch time |
| `ttl_hours` | `int` | Expiry window |
| `status` | `SessionStatus` | `active`, `expired`, or `promoted` |
| `turn_number` | `int` | Current session turn counter |

### FactNode

| Property | Type | Description |
|----------|------|-------------|
| `fact_id` | `str` | Unique fact identifier |
| `session_id` | `str` | Owning session |
| `content` | `str` | Extracted fact text |
| `fact_type` | `FactType` | Classification such as `file_state` or `decision` |
| `valid_from` | `datetime` | First-valid timestamp |
| `valid_until` | `datetime \| None` | Supersession timestamp; `None` means still active |
| `confidence` | `float` | Extractor confidence |
| `source_turn` | `int` | Turn that produced the fact |
| `embedding` | `list[float] \| None` | Optional semantic-search vector |

### FileNode

| Property | Type | Description |
|----------|------|-------------|
| `path` | `str` | File path touched during the session |
| `session_id` | `str` | Owning session |
| `last_read_turn` | `int \| None` | Last read turn |
| `last_modified_turn` | `int \| None` | Last write/edit turn |
| `status` | `FileStatus` | `read`, `modified`, `created`, or `deleted` |

### DecisionNode

| Property | Type | Description |
|----------|------|-------------|
| `decision_id` | `str` | Unique decision identifier |
| `session_id` | `str` | Owning session |
| `summary` | `str` | What was decided |
| `rationale` | `str \| None` | Optional reasoning |
| `turn` | `int` | Turn where the decision was made |
| `superseded_by` | `str \| None` | Later decision identifier when replaced |

### CheckpointNode

Single-record working-state summary for a session.

| Property | Type | Description |
|----------|------|-------------|
| `session_id` | `str` | Primary key for the session checkpoint |
| `summary` | `str` | One-line statement of current work state |
| `next_step` | `str \| None` | Highest-priority follow-up action |
| `confidence` | `float` | Extractor confidence |
| `source_turn` | `int` | Turn that produced the checkpoint |

### IssueNode

| Property | Type | Description |
|----------|------|-------------|
| `issue_id` | `str` | Unique issue identifier |
| `session_id` | `str` | Owning session |
| `status` | `str` | `"open"` or `"resolved"` |
| `summary` | `str` | Blocker or error description |
| `related_file` | `str \| None` | File path when applicable |
| `related_command` | `str \| None` | Command or verification that surfaced the issue |
| `resolution_ref` | `str \| None` | Resolution reference or note |
| `source_turn` | `int` | Turn where the issue appeared |
| `resolved_turn` | `int` | Turn where the issue was resolved; default `0` until resolved |

### VerificationNode

| Property | Type | Description |
|----------|------|-------------|
| `verification_id` | `str` | Unique verification identifier |
| `session_id` | `str` | Owning session |
| `command` | `str` | Exact command that was run |
| `status` | `str` | `"pass"`, `"fail"`, or `"partial"` |
| `summary` | `str` | Human-readable outcome |
| `source_turn` | `int` | Turn where the verification happened |

## Cache-Backed File Models

These records are backend-managed rather than first-class Pydantic models, but
they are core to curator and native-read behavior.

### FileContent

Cached full-text file content for a session.

| Property | Type | Description |
|----------|------|-------------|
| `file_id` | `str` | Backend record identifier |
| `session_id` | `str` | Owning session |
| `path` | `str` | File path |
| `content` | `str` | Full file text |
| `sha256` | `str` | Content hash for dedup/change detection |
| `line_count` | `int` | Cached line count for fast slices |
| `last_updated_turn` | `int` | Turn of the last cache write |

Ingest sources:

- read-style tool results paired by `tool_call_id`
- write/create-file tool arguments captured directly from assistant tool calls

### FileOutline

Symbol index generated from cached file content.

| Property | Type | Description |
|----------|------|-------------|
| `outline_id` | `str` | Backend record identifier |
| `session_id` | `str` | Owning session |
| `path` | `str` | File path matching the cached content entry |
| `outline` | `str` | Newline-delimited symbol list with line numbers |
| `last_updated_turn` | `int` | Turn of the last outline write |

Used by curator `get_file_outline()` before targeted `get_file_lines()` fetches.

## Graph Relationships

The logical edge vocabulary used across backends:

| Edge Type | Meaning |
|-----------|---------|
| `BELONGS_TO` | Ownership edge from a session artifact back to its session |
| `TOUCHES` | Session touched a file |
| `SUPERSEDES` | A newer fact invalidated an older fact |
| `MODIFIES` | A fact describes a file mutation |
| `SUPPORTS` | A fact supports a decision |
| `CAUSED_BY` | Causal link such as error -> follow-up state |

Backends may materialize these differently, but application code treats them as
the same logical relationships.

## Enums

### FactType

```python
class FactType(str, Enum):
    FILE_STATE = "file_state"
    ERROR = "error"
    TOOL_RESULT = "tool_result"
    DECISION = "decision"
    STATE = "state"
    GOAL = "goal"
    OBSERVATION = "observation"
```

### SessionStatus

```python
class SessionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    PROMOTED = "promoted"
```

### FileStatus

```python
class FileStatus(str, Enum):
    READ = "read"
    MODIFIED = "modified"
    CREATED = "created"
    DELETED = "deleted"
```

### PromotionOutcome

```python
class PromotionOutcome(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRY = "retry"
```

## Proxy / Extraction DTOs

### AssembledContext

What the assembler or curator returns to the chat handler.

| Property | Type | Description |
|----------|------|-------------|
| `system_message` | `dict` | Original system message |
| `graph_context` | `list[dict]` | Synthesized injected context messages |
| `coherence_tail` | `list[dict]` | Recent preserved conversation tail |
| `token_estimate` | `int` | Estimated outbound token count |
| `facts_retrieved` | `int` | Number of selected facts |
| `session_id` | `str` | Owning session |
| `files_selected` | `list[dict]` | File snippets chosen for injection |
| `decisions_selected` | `list[dict]` | Decision records chosen for injection |
| `compression_ratio` | `float` | Output-size ratio for the rewrite |
| `retained_turn_numbers` | `list[int] \| None` | Explicit middle turns kept by the curator |
| `curator_tool_log` | `list[dict]` | Per-tool call log for trace surfaces |

### ExtractionResult

Structured output from either legacy extraction or per-tool extraction.

| Property | Type | Description |
|----------|------|-------------|
| `facts` | `list[dict]` | Facts extracted from the turn |
| `files_touched` | `list[str]` | Files inferred from the turn |
| `decisions` | `list[dict]` | Decision summaries and rationales |
| `invalidated_fact_ids` | `list[str]` | Description strings to match against active facts |
| `turn_number` | `int` | Source turn |
| `session_goal` | `str \| None` | Updated or inferred session goal |
| `checkpoint` | `dict \| None` | Working-state summary block |
| `issues` | `list[dict]` | Open/resolved issues |
| `verifications` | `list[dict]` | Recorded verification results |

### TurnTrace

Primary observability record for one proxied request.

Key fields:

| Property | Type | Description |
|----------|------|-------------|
| `turn_id` | `str` | Unique trace identifier |
| `session_id` | `str \| None` | Session owning the turn |
| `turn_number` | `int` | Session turn counter |
| `model` | `str` | Upstream model name |
| `stream` | `bool` | Whether the client requested streaming |
| `input_tokens` | `int` | Estimated inbound tokens |
| `message_count` | `int` | Raw message count |
| `user_turn_count` | `int` | Number of user-role turns seen so far |
| `is_user_turn` | `bool` | Distinguishes user turns from agent-solo continuations |
| `assembly_mode` | `str` | `passthrough`, `graph`, `curator`, `agent_solo`, etc. |
| `assembly_reason` | `str` | Human-readable rationale for the chosen path |
| `rewritten_tokens` | `int` | Estimated rewritten payload size |
| `savings_tokens` | `int` | Estimated tokens saved |
| `original_messages` | `list[dict]` | Original payload snapshot |
| `rewritten_messages` | `list[dict]` | Rewritten payload snapshot |
| `facts_stored` | `int` | Facts written after extraction |
| `duplicates_skipped` | `int` | Deduped facts skipped |
| `invalidations_attempted` | `int` | Supersession descriptions produced |
| `invalidations_matched` | `int` | Supersession descriptions matched to fact ids |
| `recall_used` | `bool` | Whether recall interception executed |
| `cache_hit_tokens` | `int` | Upstream prompt-cache hits when reported |
| `cache_miss_tokens` | `int` | Upstream prompt-cache misses when reported |
| `curator_tool_log` | `list[dict]` | Curator tool-dispatch record |

### SessionTraceSummary

Aggregated trace summary per session.

| Property | Type | Description |
|----------|------|-------------|
| `session_id` | `str` | Session identifier |
| `goal` | `str \| None` | Current session goal |
| `turn_count` | `int` | Recorded turn count |
| `total_input_tokens` | `int` | Sum of observed input tokens |
| `total_savings_tokens` | `int` | Aggregate estimated savings |
| `avg_savings_ratio` | `float` | Savings over all input |
| `rewritten_savings_ratio` | `float` | Savings over rewritten turns only |
| `assembly_modes` | `dict[str, int]` | Counts by assembly mode |
| `total_facts_stored` | `int` | All stored facts |
| `total_duplicates_skipped` | `int` | All dedup skips |
| `total_invalidations_attempted` | `int` | All invalidation attempts |
| `total_recalls` | `int` | Count of recall events |
| `max_user_turns` | `int` | Highest user-turn count observed |

## Storage Layout

### GraphBackend protocol

`archolith_proxy/graph/protocol.py` defines the backend contract:

- lifecycle: connect, close, ensure_schema, verify_connectivity
- session CRUD and goal updates
- fact storage, invalidation, semantic lookup support
- file cache and file outline operations
- checkpoint, issue, verification surfaces
- cleanup / TTL expiration

### Backend-specific notes

- `Neo4jBackend` is still the code-default backend in `Settings`
- `LadybugBackend` is the preferred zero-infra local/bootstrap path
- file-content caching is richest on LadybugDB; Neo4j paths exist mainly for graph/session operations
- `TraceStore` is separate from graph storage and can optionally persist JSONL traces to disk

## Promotion Models

These live in `archolith_proxy/memory/models.py`.

### PromotionRecord

Canonical outbound payload for one promoted fact.

| Property | Type | Description |
|----------|------|-------------|
| `promotion_id` | `str` | Unique promotion identifier |
| `session_id` | `str` | Source session |
| `source_turn` | `int` | Source turn |
| `fact_type` | `str` | Fact classification |
| `content` | `str` | Durable fact text |
| `confidence` | `float` | Promotion confidence |
| `session_goal` | `str \| None` | Goal at promotion time |
| `touched_files` | `list[str]` | Related files |
| `decision_context` | `str \| None` | Decision rationale when relevant |
| `promotion_reason` | `str` | Why the fact was promoted |
| `promoted_at` | `float` | Unix timestamp |
| `tags` | `list[str]` | Classification tags |
| `dedupe_key` | `str` | Deterministic idempotency key |
| `source_trace_ref` | `str \| None` | TurnTrace linkage for auditability |

### PromotionResult

| Property | Type | Description |
|----------|------|-------------|
| `promotion_id` | `str` | Source `PromotionRecord` id |
| `engine_id` | `str` | Target engine id |
| `outcome` | `PromotionOutcome` | Attempt result |
| `remote_id` | `str \| None` | Identifier in the target memory system |
| `error_message` | `str \| None` | Failure details |
| `elapsed_ms` | `float` | Attempt latency |

### MemoryEngineConfig

| Property | Type | Description |
|----------|------|-------------|
| `id` | `str` | Unique engine identifier |
| `type` | `str` | Adapter type |
| `enabled` | `bool` | Whether the engine is active |
| `priority` | `int` | Higher value wins default selection |
| `base_url` | `str` | Engine endpoint |
| `api_key_env` | `str` | Env var containing the credential |
| `extra` | `dict` | Adapter-specific config |

### EngineCapabilities

| Property | Type | Default |
|----------|------|---------|
| `promote_fact` | `bool` | `True` |
| `promote_batch` | `bool` | `True` |
| `dedupe_lookup` | `bool` | `False` |
| `list_by_source` | `bool` | `False` |
| `update_promoted` | `bool` | `False` |
| `delete_promoted` | `bool` | `False` |
| `healthcheck` | `bool` | `True` |
