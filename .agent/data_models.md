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
| `config_overrides` | `str` | Per-session config overrides as a JSON string (base64-encoded at rest on LadybugDB; see Config in architecture.md). Empty when the session uses global config. |

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

The logical edge vocabulary used across backends (`archolith_proxy/models/graph_edges.py`):

| Edge Type | From | To | Meaning |
|-----------|------|-----|---------|
| `TOUCHES` | Session | File | Session touched a file (read or write) |
| `MODIFIES` | Session | File | Session modified a file (write/create/delete) |
| `IMPORTS` | File | File | File A imports/includes File B |
| `CAUSED_BY` | Fact | Fact | Causal link such as error → follow-up state |
| `SUPERSEDES` | Fact | Fact | A newer fact invalidated an older fact |
| `SUPPORTS` | Fact | Decision | A fact supports a decision |
| `BELONGS_TO` | Any artifact | Session | Ownership edge (node carries `session_id` field instead) |

Backends may materialize these differently, but application code treats them as
the same logical relationships. Neo4j uses label-based isolation; LadybugDB uses
foreign-key relationships with `session_id` scoping.

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
| `curator_prompt_tokens` | `int` | Curator LLM prompt tokens for this turn |
| `curator_completion_tokens` | `int` | Curator LLM completion tokens for this turn |

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
| `usage` | `dict` | Upstream LLM token usage from extraction API response |

### TurnTrace

Primary observability record for one proxied request. **67 fields total.**

**Identity:**

| Property | Type | Description |
|----------|------|-------------|
| `turn_id` | `str` | Unique trace identifier (hex[:16]) |
| `session_id` | `str \| None` | Session owning the turn |
| `turn_number` | `int` | Session turn counter |
| `trace_version` | `int` | Format version (currently 1) |
| `created_at` | `float` | Unix timestamp at record creation |

**Request:**

| Property | Type | Description |
|----------|------|-------------|
| `model` | `str` | Upstream model name |
| `stream` | `bool` | Whether the client requested streaming |
| `input_tokens` | `int` | Estimated inbound tokens |
| `message_count` | `int` | Raw message count before rewriting |
| `user_turn_count` | `int` | Number of user-role turns seen so far |
| `is_user_turn` | `bool` | Distinguishes user turns from agent-solo continuations |

**Assembly:**

| Property | Type | Description |
|----------|------|-------------|
| `assembly_mode` | `str` | `passthrough`, `graph`, `curator`, `agent_solo_compressed`, `briefing`, `briefing_stale`, etc. |
| `assembly_reason` | `str` | Human-readable rationale for the chosen path |
| `assembly_latency_ms` | `float` | Time in assembler/curator, excluding filter |

**Token economics:**

| Property | Type | Description |
|----------|------|-------------|
| `rewritten_tokens` | `int` | Estimated rewritten payload size |
| `savings_tokens` | `int` | Estimated tokens saved by rewriting |
| `savings_ratio` | `float` | Ratio of savings_tokens / input_tokens |

**Assembly decisions (from assembler/curator):**

| Property | Type | Description |
|----------|------|-------------|
| `facts_selected` | `list[dict]` | Facts injected into context |
| `files_selected` | `list[dict]` | File snippets chosen for injection |
| `decisions_selected` | `list[dict]` | Decision records chosen for injection |
| `original_messages` | `list[dict]` | Original payload snapshot |
| `original_messages_count` | `int` | Count of messages in original |
| `rewritten_messages` | `list[dict]` | Rewritten payload snapshot |

**Timing:**

| Property | Type | Description |
|----------|------|-------------|
| `request_timestamp` | `float` | Wall clock at request arrival |
| `total_latency_ms` | `float` | Total wall clock from request to response start |
| `proxy_overhead_ms` | `float` | total_latency - upstream_latency |
| `filter_latency_ms` | `float` | Time in archolith-filter; accepts alias `rtk_latency_ms` |

**Upstream response:**

| Property | Type | Description |
|----------|------|-------------|
| `upstream_status` | `int` | HTTP status from upstream |
| `upstream_latency_ms` | `float` | Time waiting for upstream response |
| `output_tokens` | `int \| None` | Tokens in upstream response (when reported) |
| `upstream_response_summary` | `str` | First 500 chars of response text |

**Extraction:**

| Property | Type | Description |
|----------|------|-------------|
| `extraction_latency_ms` | `float` | Time in fact extraction |
| `facts_stored` | `int` | Facts written after extraction |
| `duplicates_skipped` | `int` | Deduped facts skipped |
| `invalidations_attempted` | `int` | Supersession descriptions produced |
| `invalidations_matched` | `int` | Supersession descriptions matched to fact ids |
| `extracted_facts` | `list[dict]` | Facts extracted from turn |

**Helper-LLM Usage:**

| Property | Type | Description |
|----------|------|-------------|
| `extractor_prompt_tokens` | `int` | Extractor LLM prompt tokens for this turn |
| `extractor_completion_tokens` | `int` | Extractor LLM completion tokens for this turn |
| `extractor_llm_calls` | `int` | Number of extractor LLM calls this turn |
| `curator_prompt_tokens` | `int` | Curator LLM prompt tokens for this turn |
| `curator_completion_tokens` | `int` | Curator LLM completion tokens for this turn |
| `embedding_tokens` | `int` | Embedding API tokens for this turn |

**Compression:**

| Property | Type | Description |
|----------|------|-------------|
| `compression_ratio` | `float` | Output-size ratio for rewrite |

**Recall:**

| Property | Type | Description |
|----------|------|-------------|
| `recall_used` | `bool` | Whether recall interception executed |
| `recall_question` | `str` | Query string if recall was used |
| `recall_facts_returned` | `int` | Number of facts recalled |
| `recall_trigger` | `str` | Trigger source ("proxy_forced:...", "model_invoked", etc.) |

**Fallback:**

| Property | Type | Description |
|----------|------|-------------|
| `fallback_reason` | `str` | Why fallback occurred (curator timeout, etc.) |

**Upstream cache (DeepSeek prompt_cache tokens):**

| Property | Type | Description |
|----------|------|-------------|
| `cache_hit_tokens` | `int` | Upstream prompt-cache hits when reported |
| `cache_miss_tokens` | `int` | Upstream prompt-cache misses when reported |

**Curator decisions (when assembly_mode includes curator):**

| Property | Type | Description |
|----------|------|-------------|
| `curator_retained_turns` | `list[int] \| None` | Middle turns kept by turn selection (None = all) |
| `curator_context_block` | `str \| None` | The curator's assembled context text |
| `curator_tool_log` | `list[dict]` | Per-tool call dispatch log |
| `curator_failure_reason` | `str` | Why curator failed (empty on success) |

**Briefing metrics (when assembly_mode is "briefing" or "briefing_stale"):**

| Property | Type | Description |
|----------|------|-------------|
| `briefing_source_turn` | `int \| None` | Turn the briefing was built after |
| `briefing_chars` | `int` | Total chars in formatted briefing |
| `briefing_files` | `int` | Number of pre-fetched files in briefing |

**Agent-solo compression breakdown (when assembly_mode == "agent_solo_compressed"):**

| Property | Type | Description |
|----------|------|-------------|
| `solo_strategies` | `list[str]` | e.g. ["shrink", "dedup", "curator_cache"] |
| `solo_chars_saved_shrink` | `int` | Strategy A: per-result shrink savings |
| `solo_chars_saved_dedup` | `int` | Strategy B: byte-identical dedup savings |
| `solo_chars_saved_middle` | `int` | Strategy C: compressible middle-turn filter savings |
| `solo_chars_saved_compact` | `int` | Strategy D: compact argument savings |
| `solo_chars_saved_curator` | `int` | Curator prefix cache reuse savings |
| `solo_chars_saved_total` | `int` | Sum of all agent-solo strategies |

**Filter status (archolith-filter; populated every request after filter_request_body):**

| Property | Type | Description |
|----------|------|-------------|
| `filter_available` | `bool \| None` | True = package present; False = fail-open; None = not yet measured; accepts alias `rtk_available` |
| `filter_chars_saved` | `int` | Characters removed by filter on this turn; alias `rtk_chars_saved` |
| `filter_chars_before` | `int` | Characters in messages before filter; alias `rtk_chars_before` |
| `filter_chars_after` | `int` | Characters after filtering, before proxy injections; alias `rtk_chars_after` |
| `filter_strategy_savings` | `dict[str, int]` | Per-strategy breakdown; alias `rtk_strategy_savings` |
| `proxy_recall_chars_added` | `int` | Characters injected back via [PROXY-RECALL] |
| `outbound_chars_sent` | `int` | Final outbound message chars after all proxy rewrites |

**Curator eligibility:**

| Property | Type | Description |
|----------|------|-------------|
| `curator_skip_reason` | `str` | e.g. "cold_start", "disabled", "timeout", "no_api_key", "no_result" |

### BackgroundPassTrace

Record for a single background curator pass (prepper) run post-turn.

| Property | Type | Description |
|----------|------|-------------|
| `record_type` | `str` | Discriminator: always `"bg_pass"` for JSONL parsing |
| `pass_id` | `str` | Unique pass identifier (hex[:16]) |
| `session_id` | `str` | Session owning this pass |
| `trigger_turn` | `int` | Turn number that triggered this pass |
| `started_at` | `float` | Unix timestamp at pass start |
| `completed_at` | `float \| None` | Unix timestamp at pass completion (None if pending) |
| `latency_ms` | `float` | Total pass latency |
| `debounce_ms` | `float` | How long debounce sleep lasted before running |
| `outcome` | `str` | `success`, `cancelled`, `timeout`, `failed`, `no_result` |
| `cancel_reason` | `str` | Reason if cancelled (e.g., "superseded_by_next_turn") |
| `failure_detail` | `str` | Exception message if outcome is `failed` |
| `tool_calls_count` | `int` | Number of curator tool calls made |
| `tool_log` | `list[dict]` | Per-tool dispatch log (name, args, result_preview) |
| `files_fetched` | `int` | Number of files pre-fetched via `prefetch_file` |
| `context_chars` | `int` | Length of context block produced |
| `briefing_cached` | `bool` | Whether a `SessionBriefing` was written to cache |
| `prompt_tokens_used` | `int` | Curator LLM prompt tokens consumed during this pass |
| `completion_tokens_used` | `int` | Curator LLM completion tokens consumed during this pass |

### SessionTraceSummary

Aggregated trace summary per session.

| Property | Type | Description |
|----------|------|-------------|
| `session_id` | `str` | Session identifier |
| `goal` | `str \| None` | Current session goal |
| `turn_count` | `int` | Recorded turn count |
| `first_turn_at` | `float \| None` | Unix timestamp of first turn |
| `last_turn_at` | `float \| None` | Unix timestamp of last turn |
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
| `harness_env` | `dict[str, str]` | Environment metadata (extracted from system prompt `<env>` block) |
| `proxy_config` | `dict[str, object]` | Proxy feature flags and key thresholds at session start |

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

## OpenAI-Compatible Schema Models

Live in `archolith_proxy/openai/schemas.py`. **15 Pydantic models total:**

| Model | Purpose |
|-------|---------|
| `ChatMessage` | Single message in a request (system/user/assistant/tool role) |
| `ToolCallFunction` | Function name + arguments string for a tool call |
| `ToolCall` | Tool call with id, type, and function details |
| `ToolFunction` | Tool schema: name, description, parameters |
| `ToolDefinition` | Tool definition wrapper (type: "function", function: ToolFunction) |
| `ChatCompletionRequest` | OpenAI-compatible `/v1/chat/completions` request |
| `ChatMessageResponse` | Response message (assistant role with content/tool_calls) |
| `Choice` | Single choice in completion response (message + finish_reason) |
| `Usage` | Token counts (prompt, completion, total) |
| `ChatCompletionResponse` | Full non-streaming response (choices + usage + model) |
| `DeltaMessage` | Streaming delta message (role + content + tool_calls) |
| `ChunkChoice` | Streaming choice (delta + finish_reason + index) |
| `ChatCompletionChunk` | Streaming response chunk (choices + model) |
| `ModelObject` | Model info (id, owned_by, created, object) |
| `ModelListResponse` | `/v1/models` response (data: list[ModelObject]) |

## Curator Support Models

Live in `archolith_proxy/curator/`:

### CuratorResult (`curator/result.py`)

Result of a single curator loop invocation.

| Property | Type | Description |
|----------|------|-------------|
| `context_text` | `str` | Formatted context block (system message content) |
| `curated_paths` | `set[str]` | File paths the curator selected |
| `tool_calls_used` | `int` | Number of tool calls made |
| `estimated_tokens` | `int` | Rough token estimate of context_text |

### SessionBriefing (`curator/briefing.py`)

Pre-fetched context built by prepper (background pass) and injected into inline assembler.

| Property | Type | Description |
|----------|------|-------------|
| `files` | `list[PreFetchedFile]` | Files with full content, outline, and metadata |
| `key_facts` | `list[str]` | Key facts distilled from the session |
| `decisions` | `list[dict]` | Decision records with summary and rationale |

### PreFetchedFile (`curator/briefing.py`)

Single file entry in a briefing.

| Property | Type | Description |
|----------|------|-------------|
| `path` | `str` | File path |
| `content` | `str` | Full file content |
| `outline` | `str` | Symbol index (functions/classes with line numbers) |
| `line_count` | `int` | Total lines in file |
| `source_turn` | `int` | Turn the file was cached |

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
| `type` | `str` | Adapter type (see adapters below) |
| `enabled` | `bool` | Whether the engine is active |
| `priority` | `int` | Higher value wins default selection |
| `base_url` | `str` | Engine endpoint (when applicable) |
| `api_key_env` | `str` | Env var name containing the credential |
| `extra` | `dict` | Adapter-specific config |
| `resolved_api_key` (property) | `str \| None` | Resolved credential from environment (read-only) |

**Concrete adapter implementations** (`archolith_proxy/memory/adapters/`):
- `archolith_memory.py` — integration with archolith-memory library (sister project)
- `basic_memory.py` — minimal in-memory adapter for local/dev use
- `claude_mem.py` — placeholder adapter (reference only)
- `cognee.py` — Cognee knowledge graph adapter
- `generic_http.py` — HTTP POST adapter for custom backend APIs
- `mem0.py` — Mem0 memory service adapter
- `nocturne_memory.py` — Nocturne memory adapter
- `openmemory.py` — OpenMemory protocol adapter
- `zep.py` — Zep memory service adapter

**Base contract** (`adapters/base.py`):
- `validate_config()` — check engine config validity
- `capabilities()` → `EngineCapabilities`
- `healthcheck()` → `bool`
- `promote_fact()` → `PromotionResult`
- `promote_batch()` (optional)
- `dedupe_lookup()` (optional)
- `list_by_source()` (optional)
- `update_promoted()` (optional)
- `delete_promoted()` (optional)

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

## Plugin System Models

### ProxyPlugin Protocol (`archolith_proxy/plugins/registry.py`)

`@runtime_checkable` Protocol. Six required members:

| Member | Type | Description |
|--------|------|-------------|
| `plugin_id` | `str` (property) | Unique ID — `"filter"`, `"audit"`, `"memory"` |
| `plugin_version` | `str` (property) | Semantic version string |
| `activate()` | `async -> bool` | Startup hook. True = ready, False = degraded, raise = error |
| `deactivate()` | `async -> None` | Shutdown hook. Best-effort. |
| `healthcheck()` | `async -> dict` | Returns `{"status": "ok"|"degraded"|"unavailable", ...}` |
| `contribute_metrics()` | `-> dict[str, int\|float]` | Flat counters for `/metrics`; must not block |

### PluginRegistry

Process-level singleton (`get_plugin_registry()`). Tracks plugin instances and their lifecycle statuses.

| Status | Meaning |
|--------|---------|
| `inactive` | Registered but not yet activated, or disabled by config |
| `active` | `activate()` returned `True` |
| `degraded` | `activate()` returned `False` |
| `error` | `activate()` raised, or version below `MIN_PLUGIN_VERSIONS` |

### MIN_PLUGIN_VERSIONS

```python
MIN_PLUGIN_VERSIONS: dict[str, str] = {
    "filter": "0.1.0",
    "audit": "0.1.0",
    "memory": "0.1.0",
}
```

Version check runs before `activate()`. Mismatch → status `error`, proxy still starts.
