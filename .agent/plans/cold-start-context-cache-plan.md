# Cold Start Context Cache Plan

**Status:** Proposed  
**Author:** Arena Agent  
**Date:** 2026-07-19  
**Related to:** Prompt Cache Stability Plan (item #12)  
**Goal:** Reduce the cost penalty on the very first turn of a session by establishing an early cache entry.

---

## Problem Statement

On the first turn of any session, the context cache is empty by definition. This means:

- The proxy must always render fresh on turn 1.
- The upstream LLM sees a completely new prompt prefix.
- The user pays the full uncached price for the first (often largest) request.
- Subsequent turns may benefit from caching, but the first turn is always expensive.

For long-running coding sessions (especially with Reasonix on DeepSeek or other cache-aware providers), the first turn can be one of the most expensive because it contains the full system prompt + initial context.

**Current behavior:** Turn 1 always pays full price.  
**Desired behavior:** Turn 1 still pays full price, but we establish a strong cache entry as early as possible so turns 2+ get maximum discount.

---

## Goals

| Goal | Target | Measurement |
|------|--------|-------------|
| Establish early cache entry | A usable cached context block exists by the end of turn 1 or early in turn 2 | `context_cache_entries_created_on_turn_1` |
| Minimal first-turn latency impact | Pre-warming adds < 150ms on turn 1 | Instrumented latency |
| High hit rate on turn 2+ | ≥ 70% of sessions get a cache hit on turn 2 | `context_cache_hit_rate` filtered by turn |
| No quality regression | First-turn context quality remains identical | Manual + benchmark review |

---

## Proposed Approaches

### Option A: Lightweight Pre-warm on Turn 1 (Recommended)

On the first user turn:

1. Run a **very lightweight** version of the deterministic assembler (or a minimal briefing).
2. Use a simplified signature based only on `session_goal + first_user_message_fingerprint`.
3. Render and store the context block **before** processing the full request.
4. Use the pre-warmed block for the actual upstream call.

**Pros:**
- Turn 2 has a very high chance of hitting the cache.
- Low complexity.

**Cons:**
- Adds a small amount of work on turn 1.
- The initial signature may be too broad (many later turns may still miss).

### Option B: Background Pre-warm After Turn 1

After the first turn completes:

1. Asynchronously render a baseline context block using the first user message + initial files touched.
2. Store it for future turns.

**Pros:**
- Zero impact on turn 1 latency.
- Can use more accurate information (actual files touched on turn 1).

**Cons:**
- Turn 2 may still miss if the background job hasn't finished.
- More complex scheduling.

### Option C: Use First Turn as the Cache Seed (Simplest)

Do nothing special on turn 1. After the first successful render, immediately store the result with a broad signature. This is effectively what happens today if we enable the main cache.

**Pros:** Zero new code.  
**Cons:** Turn 2 may still miss if the signature is too strict.

**Recommendation:** Start with **Option A** (lightweight pre-warm) as it gives the best balance of early cache establishment with minimal complexity.

---

## Technical Details

### Pre-warm Signature (Turn 1)

```python
def compute_cold_start_signature(
    session_goal: str,
    first_user_message: str,
) -> str:
    key = f"cold_start|{session_goal}|{first_user_message[:150]}"
    return hashlib.sha256(key.encode()).hexdigest()
```

This signature is intentionally broader than the normal multi-turn signature so it has a higher chance of being reusable on turn 2.

### Storage

Use the same `context_cache` table as the main plan, but mark entries with `is_cold_start=True` or a special `source="cold_start"`.

### Integration Point

- Add a new function `maybe_pre_warm_context()` in `curator/pipeline.py`.
- Call it early in the first-turn path (before the main curator/deterministic run).
- If a pre-warmed block exists and is fresh enough, use it.

---

## Metrics

- `context_cache_cold_start_entries_created`
- `context_cache_cold_start_hit_rate` (how often turn 2+ hits a cold-start entry)
- `context_cache_cold_start_latency_ms` (added latency on turn 1)

---

## Risks & Trade-offs

| Risk | Mitigation |
|------|------------|
| Added latency on first turn | Keep pre-warm extremely lightweight (minimal briefing, no LLM) |
| Overly broad signature causes bad context | Use a conservative signature and let the normal (stricter) signature take over on later turns |
| Pre-warm produces lower quality context | Only use it as a seed; allow normal refresh on turn 2 if needed |

---

## Relationship to Main Prompt Cache Stability Plan

- This plan is a **companion** to the main Prompt Cache Stability Plan.
- The main plan focuses on ongoing turns; this plan focuses on the critical first turn.
- Both plans share the same storage layer (`context_cache` table) and signature infrastructure.
- Implementation of this plan should happen **after** the core caching mechanism (Phase 0–1 of the main plan) is in place.

---

## Open Questions

1. Should the cold-start signature include any file information from the first turn, or stay purely goal + message based?
2. Should we support a configurable “pre-warm budget” (token or time limit) for the initial render?
3. Do we want to expose `cold_start_pre_warm_enabled` as a separate flag, or tie it to `context_cache_enabled`?

---

## Next Steps (if approved)

1. Create this as a standalone plan (done).
2. Implement after the core context cache is working.
3. Add cold-start specific metrics and a simple pre-warm path.
4. Measure impact on turn-2 cache hit rate in benchmarks.

This addresses the “first turn is always expensive” problem that would otherwise limit the overall cost savings of the prompt cache stability effort.