# Prompt Cache Stability Plan

**Status:** Proposed  
**Author:** Arena Agent (based on senior-dev review)  
**Date:** 2026-07-19  
**Related Review Item:** #6 (Corrected interpretation)  
**Goal:** Dramatically reduce per-turn token cost by maximizing upstream LLM prompt cache hits.

---

## Problem Statement

The archolith-context proxy rebuilds the system prompt on nearly every user turn. Even when using the deterministic assembler, the rendered context block (`=== SESSION GOAL ===`, `=== RELEVANT CODE ===`, `=== KEY FACTS ===`, etc.) tends to change in subtle ways between turns:

- Different files selected or different ordering
- Slightly different fact text or fact count
- Non-deterministic rendering (e.g., insertion order, varying section lengths)
- Timestamps, turn numbers, or other volatile elements

Because the large prefix of the prompt changes, upstream providers (especially DeepSeek with prefix caching and OpenAI models with prompt caching) treat the request as a **new prompt**. This causes the user to pay the full uncached price instead of receiving the large cache discount on repeated prefixes.

This is particularly painful for long coding sessions with Reasonix (DeepSeek-native) and any other harness using cache-aware providers.

**Current behavior:** High cache miss rate → full price on every turn.  
**Desired behavior:** Stable system prompt prefix → high cache hit rate → significantly lower per-turn cost.

---

## Goals & Success Criteria

| Goal | Target | Measurement |
|------|--------|-------------|
| Maximize prefix stability | ≥ 85% of turns should produce an identical (or byte-identical) system message prefix | `curator_cache_hit_rate` + upstream `cached_tokens` |
| Reduce effective cost | 40–70% reduction in average input cost per turn on cache-aware models | Compare `prompt_tokens` vs `cached_tokens` in traces |
| Preserve context quality | No regression in task completion or navigation quality | archolith-bench context_quality_ab |
| Minimal latency impact | Cache check must be < 5ms | Instrumented in `curate_context` / deterministic path |
| Backward compatible | Existing behavior when feature is disabled | Feature flag + profile gating |

---

## Proposed Approach

### Core Idea

Treat the **rendered context block** (the large system message we inject) as a cacheable artifact. On each turn:

1. Compute a **stable context signature** from the briefing + current user message.
2. Check if we have a previously rendered context block for that signature.
3. If hit → reuse the cached block (cheap read, high chance of upstream cache hit).
4. If miss → render (deterministic or curator), store the result, and use it.

The cache is **append-only** at the session level (new signatures get new entries; old ones are never mutated).

### Key Design Decisions

1. **Cache Key**
   - Primary: `sha256(session_goal + sorted(touched_files) + last_user_message_fingerprint[:200])`
   - Optional secondary: include `briefing.source_turn` for freshness.

2. **What We Cache**
   - The final rendered system message content (`context_block`)
   - The `files_selected` list with metadata
   - The `AssembledContext` object (or a minimal serializable form)

3. **Storage**
   - Reuse/extend the existing `curator/persistence.py` SQLite layer (already used for `curator_state_persist`).
   - New table: `context_cache` (session_id, signature, rendered_block, files_selected, created_turn, tokens).

4. **Invalidation**
   - On goal change
   - On significant file set change (new files added that weren't in the previous signature)
   - Explicit TTL or max entries per session (safety)

5. **Integration Points**
   - `curator/pipeline.py` — check cache before running prepper/assembler
   - `curator/deterministic_assembler.py` — primary path (cheaper to cache)
   - `curator/loop.py` — secondary path for full curator results

---

## Implementation Phases

### Phase 0 — Foundation (Low Risk)
- Add `context_cache_enabled` flag + config group entry
- Create the `context_cache` table in the persistence layer
- Add helper functions: `compute_context_signature()`, `get_cached_context()`, `store_context()`

### Phase 1 — Deterministic Path (High Impact)
- Wire cache check into `run_deterministic_assembler`
- On hit: return cached `AssembledContext` directly (skip all rendering)
- On miss: render → store → return
- Add metrics: `deterministic_cache_hits`, `deterministic_cache_misses`

### Phase 2 — Full Curator Path
- Extend caching to the LLM curator path (more complex because of tool calls)
- Optional: cache the *briefing* itself when the signature matches

### Phase 3 — Observability & Tuning
- Surface cache hit rate and `cached_tokens` savings in `/metrics` and dashboard
- Add trace field `context_cache_hit`
- Experiment with signature components (how much of the user message to include)

---

## Technical Details

### Signature Function (proposed)

```python
def compute_context_signature(
    session_goal: str,
    touched_files: list[str],
    user_message: str,
    briefing_hash: str | None = None,
) -> str:
    key = f"{session_goal}|{','.join(sorted(touched_files))}|{user_message[:200]}"
    if briefing_hash:
        key += f"|{briefing_hash}"
    return hashlib.sha256(key.encode()).hexdigest()
```

### Storage Schema (SQLite)

```sql
CREATE TABLE context_cache (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    rendered_block TEXT NOT NULL,
    files_selected_json TEXT,
    created_turn INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, signature)
);
```

### Invalidation Rules

- Delete entries when `session_goal` changes
- Optional: keep only the last N signatures per session (LRU-style)
- Manual invalidation endpoint for operators (`DELETE /admin/sessions/{id}/context_cache`)

---

## Risks & Trade-offs

| Risk | Mitigation |
|------|------------|
| Stale context served | Signature includes `touched_files` + optional `briefing.source_turn`; invalidate on goal change |
| Cache grows unbounded | Per-session cap + TTL; reuse existing cleanup loop |
| Signature too strict (low hit rate) | Start conservative; tune via experiment (user_message length, file set vs file content hash) |
| Cache key collision | SHA-256 is sufficient; add version prefix if needed |
| Complexity in full curator path | Phase 1 limits scope to deterministic assembler first |

---

## Success Metrics (to instrument)

- `context_cache_hits` / `context_cache_misses`
- `context_cache_hit_rate`
- `context_cache_tokens_saved` (estimated)
- Upstream `cached_tokens` delta (from `TurnTrace`)
- `deterministic_cache_hits` (Phase 1)

---

## Dynamic Cost Trade-off (New Requirement)

Even with excellent cache stability, there comes a point where **riding a bloated cached prompt is more expensive** than accepting a cache miss to send a fresher, smaller prompt.

### The Economic Crossover Problem

- A cached prompt may contain many old file excerpts, superseded facts, and verbose sections.
- A fresh deterministic render might be significantly smaller (especially after file supersession pruning).
- At some token threshold, paying the full uncached price for a smaller prompt becomes cheaper than continuing to pay the cached rate on a bloated prompt.

### Proposed Solution: Cost Crossover Detector

Add a lightweight decision layer before serving a cache hit:

```python
def should_use_cached_context(
    cached_tokens: int,
    estimated_fresh_tokens: int,
    cache_hit_discount: float = 0.1,   # e.g. 10% of normal price
) -> bool:
    cached_cost = cached_tokens * cache_hit_discount
    fresh_cost = estimated_fresh_tokens * 1.0
    return cached_cost < fresh_cost
```

This check runs on every potential cache hit. If the cached version is > X% larger than the estimated fresh version (configurable), we **force a miss** and render fresh.

**Config knobs:**
- `context_cache_max_bloat_ratio: float = 1.6` (if cached > 1.6× fresh estimate → force refresh)
- `context_cache_force_refresh_threshold_tokens: int = 12000`

This turns the system into a true cost optimizer rather than a pure cache maximizer.

---

## File Supersession & Staleness Awareness (New Requirement)

Even when the signature matches, the **content** inside the cached block can become stale.

### The Supersession Problem

- File `src/Page.tsx` was read at turn 12 → excerpt cached.
- At turn 18 the agent edits `src/Page.tsx`.
- The cached excerpt is now out of date.
- A newer read of the same file should take precedence.

### Proposed Solution: File Version Tracking in Cache

1. When storing a context block, also store a lightweight **file version map**:
   ```json
   {
     "src/Page.tsx": {"last_read_turn": 12, "content_hash": "abc123"},
     "src/utils.ts":  {"last_read_turn": 15, "content_hash": "def456"}
   }
   ```

2. On a potential cache hit:
   - Compare the current briefing’s file metadata against the cached version map.
   - If any file has a newer `last_read_turn` **or** different content hash → treat as stale and force a cache miss (or partial refresh).

3. Partial refresh strategy (future optimization):
   - Instead of discarding the entire cached block, only re-render the `RELEVANT CODE` section for the changed files while keeping the rest of the cached prompt.

This ensures correctness while still preserving most of the cache benefit.

---

## Updated Invalidation Rules

In addition to the original rules, add:

- **File supersession check** (see above)
- **Bloat ratio check** (see Dynamic Cost Trade-off)
- **Goal drift detection** (if session goal embedding drifts significantly)

---

## Updated Success Metrics

Add:
- `context_cache_forced_refresh_bloat` — times we rejected a cache hit due to bloat
- `context_cache_forced_refresh_stale_file` — times we rejected due to file supersession
- `context_cache_cost_savings_vs_fresh` — estimated dollars saved vs always rendering fresh

---

## Open Questions (Updated)

1. Should the cache also store the *briefing* (so we can skip the prepper on a hit)?
2. How aggressive should we be with the user message portion of the signature?
3. Do we want a global (cross-session) cache for very stable goals?
4. Should we expose `context_cache_signature_components` and `context_cache_max_bloat_ratio` as tunable knobs?
5. Should we implement partial refresh (only re-render changed files) or always do a full re-render on staleness?

---

## Additional Considerations

### 1. Signature Versioning & Evolution
- The signature algorithm may need to evolve (e.g., adding file content hashes, goal embedding, etc.).
- Store a `signature_version` alongside each cache entry so old signatures can still be matched while new logic runs.
- Plan for a migration path when the signature function changes significantly.

### 2. Partial / Incremental Refresh
- Instead of a full cache miss on staleness, implement **delta rendering**:
  - Identify only the changed files/sections.
  - Re-render just the `RELEVANT CODE` portion.
  - Splice the fresh section into the previously cached block.
- This preserves most of the prompt prefix for maximum cache benefit.

### 3. Interaction with Coherence Tail
- The coherence tail (last N messages) is kept verbatim and is **not** part of the cached system block.
- However, very recent file reads in the tail can make the cached system block stale.
- Consider a "tail-aware" signature that includes the last 1–2 tool results when they contain file content.

### 4. Security & Privacy
- Cached context blocks may contain sensitive code, file contents, or extracted facts.
- The cache lives in the same SQLite file as other session state.
- Consider:
  - Encryption at rest for the `context_cache` table (optional, behind flag).
  - Automatic redaction of high-sensitivity patterns before caching.
  - Respecting `LOG_PII_REDACTION_LEVEL` when storing.

### 5. Multi-Process / Distributed Consistency
- When running multiple proxy instances (e.g., with `curator_worker_lease_enabled`), different processes may write to the same session’s context cache.
- The existing lease mechanism helps, but we should ensure the context cache writes are also serialized or use optimistic concurrency.

### 6. Observability & Explainability
- Add a `cache_decision_reason` field to traces:
  - `hit`
  - `miss_signature`
  - `miss_bloat`
  - `miss_stale_file`
  - `miss_goal_change`
- Expose this in the dashboard so operators can understand why the proxy chose to render fresh vs reuse cache.

### 7. Fallback & Circuit Breaker
- If the context cache SQLite becomes slow or corrupted, the system should gracefully degrade to always rendering fresh (fail-open).
- Add a circuit breaker around cache operations similar to the existing synthetic tool circuit breaker.

### 8. A/B Testing & Experimentation
- Make it easy to run experiments:
  - `context_cache_mode: off | conservative | aggressive | cost_optimized`
  - Different signature strategies
  - Different bloat thresholds
- Wire this into the existing benchmark harness so we can measure real cost impact.

### 9. Interaction with Other Features
- **Agent-solo compression**: Does a compressed agent-solo turn still benefit from the cached system prompt? (Likely yes.)
- **Recall tool**: If the model explicitly calls `__archolith_recall`, should that force a cache miss?
- **Long-term memory promotion**: Promoted facts might need to be excluded from (or specially handled in) the cache.
- **Per-tool extraction**: More structured facts may produce more stable signatures.

### 10. Token Accounting Accuracy
- When serving a cache hit, the proxy still needs to report accurate token counts to the trace.
- The upstream may report `cached_tokens`, but the proxy’s internal accounting must remain correct for billing dashboards and the session token budget.

### 11. Cache Eviction Policy
- Beyond per-session caps, consider a global LRU or size-based eviction for very long-lived sessions.
- Should very old cache entries (e.g., > 500 turns) be automatically dropped even if the session is still active?

**Provider Cache TTL Awareness (Critical Addition)**  

Research as of 2026 shows the following typical prompt cache lifetimes:

| Provider       | Typical TTL                          | Notes |
|----------------|--------------------------------------|-------|
| **OpenAI**     | 5–10 minutes (up to 1h off-peak, 24h with `prompt_cache_retention=long`) | Automatic, no explicit control in most cases |
| **Anthropic**  | 5 minutes (default), 1 hour (opt-in) | Refreshes on access; explicit `cache_control` |
| **DeepSeek**   | Best-effort, “hours to days”         | Disk-backed in some models; no SLA |
| **Gemini**     | 1 hour (configurable)                | Explicit caching with storage billing |
| **AWS Bedrock**| 5 min / 30 min / 1 hour              | Depends on model family |

**Implication**:  
Even if our internal `context_cache` still has a valid entry, if the upstream provider’s cache has already expired, serving the old block will **not** give us the cheap cached rate. In this case, we should treat it as a miss and re-render fresh so we can establish a new cache entry on the provider side.

**Proposed behavior**:
- Store `last_used_at` on each cache entry.
- On a potential hit, check the age of the entry against a configurable `provider_cache_ttl` (default per provider, overridable via `UPSTREAM_PROVIDER` or explicit setting).
- If the internal cache entry is older than the provider’s expected TTL → force a miss (and log `miss_provider_cache_expired`).

This makes our internal cache aware of the real economics of the upstream cache. We should also expose `provider_cache_ttl_seconds` as a setting so operators can tune it per deployment.

### 12. Cold Start Behavior
**This item has been extracted into its own dedicated plan:**

→ [Cold Start Context Cache Plan](cold-start-context-cache-plan.md)

The new plan covers pre-warming strategies, metrics, risks, and integration with the main caching effort.

---

## Relationship to Existing Work

- Builds directly on the deterministic assembler work (#2–#5)
- Reuses the persistence infrastructure added for curator state durability
- Complements the task-ranked map (stable ordering helps cacheability)
- Does **not** conflict with per-tool extraction or other future roadmap items

---

## Next Steps (if approved)

1. Create this plan as an official `.agent/plans/` document
2. Implement Phase 0 + Phase 1 on the `arena/019f78b5-archolith-context` branch
3. Add basic tests and metrics
4. Run archolith-bench context_quality_ab + cost comparison

This change has the potential to be one of the highest-ROI experimental improvements in the project because it directly attacks the token cost problem at the provider boundary rather than just inside the proxy.