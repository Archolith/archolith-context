# Trace Format Reference

This document describes the JSONL trace format for archolith-context session observability.

## Overview

Traces are stored as JSONL (one JSON record per line) in session-specific files.
Each record captures telemetry for:
- Single proxy turns (`TurnTrace`)
- Background curator passes (`BackgroundPassTrace`)

Traces serve as the primary inspection artifact: operators can answer "what did the proxy do on this turn?" without reading logs or querying the graph backend.

## Storage

### File location
Default: `./data/traces/` (configurable via `TRACE_STORE_PATH`)

### Naming convention
Files are named `<session_id>.jsonl` where `session_id` is sanitized:
- Non-hex, non-UUID characters are replaced with underscores
- Example: session `abc-def-123` → filename `abc_def_123.jsonl`

Known non-session files (skipped during load):
- `curator_failures.jsonl` — failed curator invocation audit log
- `*.tmp` — temporary/incomplete records

### Retention policy
- Default: keep 30 days (configurable via `TRACE_RETENTION_DAYS`)
- Old records are periodically cleaned up by `trace/store.py`
- Cleanup runs as a background task (interval configurable)

## Per-Line JSONL Schema

### TurnTrace Record

One record per proxy request. **Record type: implicit (TurnTrace is default).**

**Discriminator fields:**
- No `record_type` field → TurnTrace (legacy default)
- `record_type` == "bg_pass" → BackgroundPassTrace

**Full field inventory (67 fields):**

```json
{
  "turn_id": "a1b2c3d4e5f6g7h8",
  "session_id": "session_abc123",
  "turn_number": 5,
  "trace_version": 1,
  "created_at": 1681234567.89,
  
  "model": "gpt-4-turbo",
  "stream": false,
  "input_tokens": 2048,
  "message_count": 12,
  "user_turn_count": 3,
  "is_user_turn": true,
  
  "assembly_mode": "curator",
  "assembly_reason": "User turn, curator enabled, past cold-start",
  "assembly_latency_ms": 850.5,
  
  "rewritten_tokens": 1024,
  "savings_tokens": 1024,
  "savings_ratio": 0.5,
  
  "facts_selected": [
    {"fact_id": "f123", "content": "...", "type": "decision"},
    {"fact_id": "f124", "content": "...", "type": "file_state"}
  ],
  "files_selected": [
    {"path": "src/main.py", "lines": "1-50", "content": "..."}
  ],
  "decisions_selected": [
    {"decision_id": "d1", "summary": "Use async for I/O"}
  ],
  
  "original_messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "original_messages_count": 12,
  "rewritten_messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  
  "request_timestamp": 1681234567.0,
  "total_latency_ms": 3500.0,
  "proxy_overhead_ms": 2100.0,
  "filter_latency_ms": 500.0,
  
  "upstream_status": 200,
  "upstream_latency_ms": 1400.0,
  "output_tokens": 512,
  "upstream_response_summary": "I'll refactor the auth module...",
  
  "extraction_latency_ms": 450.0,
  "facts_stored": 3,
  "duplicates_skipped": 1,
  "invalidations_attempted": 2,
  "invalidations_matched": 2,
  "extracted_facts": [
    {"fact_id": "f125", "type": "decision", "content": "..."}
  ],
  
  "compression_ratio": 0.5,
  
  "recall_used": false,
  "recall_question": "",
  "recall_facts_returned": 0,
  "recall_trigger": "",
  
  "fallback_reason": "",
  
  "cache_hit_tokens": 0,
  "cache_miss_tokens": 0,
  
  "curator_retained_turns": [2, 5, 8],
  "curator_context_block": "=== SESSION GOAL ===\nFix auth module...",
  "curator_tool_log": [
    {
      "tool_name": "get_checkpoint",
      "tool_call_id": "tc_1",
      "result_preview": "Current state: refactoring...",
      "raw_result": "..."
    }
  ],
  "curator_failure_reason": "",
  
  "briefing_source_turn": 4,
  "briefing_chars": 5000,
  "briefing_files": 3,
  
  "solo_strategies": ["curator_cache", "dedup"],
  "solo_chars_saved_shrink": 100,
  "solo_chars_saved_dedup": 200,
  "solo_chars_saved_middle": 0,
  "solo_chars_saved_compact": 150,
  "solo_chars_saved_curator": 400,
  "solo_chars_saved_total": 850,
  
  "filter_available": true,
  "filter_chars_saved": 300,
  "filter_chars_before": 5000,
  "filter_chars_after": 4700,
  "filter_strategy_savings": {
    "shrink_tool_results": 200,
    "filter_output": 100
  },
  "proxy_recall_chars_added": 0,
  "outbound_chars_sent": 4700,
  
  "curator_skip_reason": ""
}
```

### BackgroundPassTrace Record

One record per background curator pass (prepper). **Discriminator: `record_type == "bg_pass"`.**

```json
{
  "record_type": "bg_pass",
  
  "pass_id": "bgp_x1y2z3a4b5c6",
  "session_id": "session_abc123",
  "trigger_turn": 5,
  
  "started_at": 1681234570.0,
  "completed_at": 1681234575.5,
  "latency_ms": 5500.0,
  "debounce_ms": 2000.0,
  
  "outcome": "success",
  "cancel_reason": "",
  "failure_detail": "",
  
  "tool_calls_count": 8,
  "tool_log": [
    {
      "tool_name": "get_checkpoint",
      "tool_call_id": "tc_bp_1",
      "result_preview": "Current state: refactoring...",
      "raw_result": "..."
    },
    {
      "tool_name": "score_file_relevance",
      "tool_call_id": "tc_bp_2",
      "result_preview": "Files by relevance: ...",
      "raw_result": "..."
    }
  ],
  
  "files_fetched": 3,
  "context_chars": 12000,
  "briefing_cached": true
}
```

## Parsing Notes

### Session-id resolution
When loading traces, `load_from_disk(session_id)` accepts:
- **Hex session IDs** (recent standard): normalized directly
- **UUID session IDs**: parsed and stored
- **Non-hex IDs** (permissive fallback): matches sanitized filenames

This is intentionally loose to support legacy sessions and operator-assigned session IDs.

### Filtering out non-session files
During batch loads (`load_all_sessions()`), files matching these patterns are skipped:
- `curator_failures.jsonl` — curator error audit log (not a session trace)
- `*.tmp` — temporary/incomplete writes

### Record type detection
- Missing `record_type` field → TurnTrace (backward-compatible default)
- `record_type == "bg_pass"` → BackgroundPassTrace
- Other values → log warning, treat as unknown

## Field Aliases (Backward compatibility)

The following field names are accepted as aliases for newer names:

| Old Name | New Name | Context |
|----------|----------|---------|
| `rtk_latency_ms` | `filter_latency_ms` | Renamed when archolith-filter became primary |
| `rtk_available` | `filter_available` | Filter availability indicator |
| `rtk_chars_saved` | `filter_chars_saved` | Filter characters saved |
| `rtk_chars_before` | `filter_chars_before` | Filter input size |
| `rtk_chars_after` | `filter_chars_after` | Filter output size |
| `rtk_strategy_savings` | `filter_strategy_savings` | Filter breakdown by strategy |

Trace records using old names are automatically normalized on deserialization via Pydantic `validation_alias` mappings.

## Schema Version

Current: `trace_version == 1`

When the schema evolves, increment `trace_version` and document migration rules.

## Querying Traces

### Via dashboard
- Interactive UI at `GET /static/dashboard.html`
- Timeline view with per-turn metrics
- Drill-down into original/rewritten messages, curator tool log
- Background pass timeline (parallel lane)

### Via CLI
```bash
# List all sessions with traces
ls -la data/traces/*.jsonl

# View a session's trace summary
python -c "
from archolith_proxy.trace import TraceStore
store = TraceStore('data/traces/')
summary = store.get_session_summary('session_abc123')
print(summary.model_dump_json(indent=2))
"

# Query specific turns
jq '.[] | select(.turn_number == 5)' data/traces/session_abc123.jsonl
```

### Via Python API
```python
from archolith_proxy.trace import TraceStore

store = TraceStore(path='data/traces/')

# Load a session's full trace history
turns = store.load_from_disk(session_id='session_abc123')
for turn in turns:
    if turn.record_type == 'bg_pass':
        print(f"Background pass: {turn.outcome}")
    else:
        print(f"Turn {turn.turn_number}: {turn.assembly_mode}")

# Get aggregated summary
summary = store.get_session_summary('session_abc123')
print(f"Total savings: {summary.total_savings_tokens} tokens")
print(f"Modes: {summary.assembly_modes}")
```

## Observability Patterns

### Token economics
- Track `savings_ratio` per turn; aggregate via `SessionTraceSummary.avg_savings_ratio`
- Compare `input_tokens` vs `rewritten_tokens` to measure assembly impact
- Use `filter_chars_saved` to measure archolith-filter contribution

### Curator behavior
- Filter turns by `assembly_mode == "curator"` to see when curator was used
- Check `curator_skip_reason` to diagnose curator opt-outs (cold start, timeout, disabled)
- Review `curator_tool_log` for unexpected tool sequences or repeated failures

### Background pass quality
- Check `briefing_cached == true` to confirm prepper succeeded
- Track `latency_ms` vs `BACKGROUND_PASS_LATENCY_BUDGET_MS` (default 30s) to detect timeouts
- Count `files_fetched` to validate anticipation accuracy

### Filter effectiveness
- `filter_available == true` confirms archolith-filter is installed and working
- `filter_strategy_savings` breakdown shows which strategies worked best
- Compare `filter_chars_saved` to total compression savings (`solo_chars_saved_total`) to gauge filter contribution

### Session health
- `fallback_reason` non-empty → curator failed, fell back to passthrough
- `duplicates_skipped` high → potential dedup over-aggressiveness
- `invalidations_matched` low relative to `invalidations_attempted` → weaker fact supersession detection
