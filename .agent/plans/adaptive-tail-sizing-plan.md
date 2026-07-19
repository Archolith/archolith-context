# Adaptive Tail Sizing by Intent

**Status:** Proposed  
**Author:** Arena Agent  
**Date:** 2026-07-19  
**Related Review Item:** Adaptive tail sizing by intent (Medium effort, Medium-High value)  
**Goal:** Dynamically adjust the size of the coherence tail based on the intent of the current user turn, improving context quality for both "continue" and "pivot" turns.

---

## Problem Statement

The current coherence tail size (`COHERENCE_TAIL_SIZE`) is a **static** configuration value (default = 10).

This one-size-fits-all approach has limitations:

- On **continuation turns** ("continue what we were doing", "fix the failing test", "now do the same for the other file"), the model often benefits from seeing **more** recent history.
- On **pivot turns** ("let's start on a new feature", "ignore everything above", "start fresh"), a smaller tail is often better to avoid polluting the context with irrelevant history.

A fixed tail size cannot optimally serve both cases.

---

## Goals

| Goal | Target | Measurement |
|------|--------|-------------|
| Improve context quality on continuation turns | Higher success rate on multi-turn continuation tasks | archolith-bench context_quality_ab |
| Reduce noise on pivot turns | Fewer irrelevant facts in the tail | Manual review + trace analysis |
| Low implementation cost | Minimal changes to existing tail logic | Lines changed + test coverage |
| Configurable behavior | Operators can tune sensitivity | New config knobs |

---

## Proposed Approach

Add an **intent classification** step before computing the coherence tail.

1. Analyze the last user message for continuation vs pivot signals.
2. Adjust the effective `base_size` passed to `smart_tail()`:
   - **Continue** → `base_size + delta` (e.g. +4)
   - **Pivot** → `max(base_size - delta, min_size)` (e.g. -3)
   - **Neutral** → `base_size` (no change)

The adjustment is applied **before** the existing tool-call integrity logic in `smart_tail()`.

---

## Intent Classification Strategy

We will use a **lightweight, rule-based classifier** (no LLM call on the hot path).

### Signals for "Continue" intent
- Phrases: "continue", "keep going", "do the same", "fix the", "now do", "also", "next"
- References to previous work ("the failing test", "that file", "what we were doing")

### Signals for "Pivot" intent
- Phrases: "start fresh", "new feature", "ignore", "forget", "start over", "different approach"
- Clear topic change

### Implementation

Add a new function in `assembler/tail.py`:

```python
def classify_turn_intent(user_message: str) -> Literal["continue", "pivot", "neutral"]:
    ...
```

Then modify the tail assembly path to call this before `smart_tail()`.

---

## Configuration

New settings (added to `ProxyBehaviorGroup` or `CuratorGroup`):

```python
tail_intent_adjustment: int = 4          # How much to expand/shrink
tail_min_size: int = 3                   # Minimum tail size after shrinking
tail_intent_enabled: bool = False        # Feature flag (off by default)
```

---

## Implementation Phases

### Phase 0 – Classifier + Config
- Add `classify_turn_intent()` function
- Add config flags (`tail_intent_enabled`, `tail_intent_adjustment`, `tail_min_size`)
- Wire the classifier into the tail selection path

### Phase 1 – Integration
- Modify the place where `smart_tail()` is called (likely in `proxy/rewrite.py` or `assembler/tail.py`)
- Apply adjusted base size only when `tail_intent_enabled=True`

### Phase 2 – Observability
- Add trace field: `tail_intent` (`continue` / `pivot` / `neutral`)
- Add metric: `tail_intent_adjustments`
- Log the effective tail size when adjustment is applied

### Phase 3 – Tuning & Testing
- Add regression tests for the classifier
- Run archolith-bench with the feature enabled

---

## Risks & Trade-offs

| Risk | Mitigation |
|------|------------|
| Over-expansion on continuation turns | Cap expansion with `max_size` |
| Classifier false positives | Start conservative; make tunable |
| Added latency | Rule-based classifier is extremely fast (<1ms) |

---

## Success Metrics

- `tail_intent_adjustments` (count of times adjustment was applied)
- Change in context quality scores on continuation vs pivot scenarios
- Operator feedback on tail behavior

---

## Open Questions

1. Should the classifier also consider the previous assistant message?
2. Should we support a "strong continue" vs "weak continue" distinction?
3. Do we want to expose the classifier as a plugin point later?

---

## Relationship to Existing Work

- Builds on the **smart_tail** improvements done earlier in the project.
- Complements the **Prompt Cache Stability** work (more stable recent context helps cache hits).
- Works well alongside the **deterministic assembler**.

---

## Next Steps (if approved)

1. Create this plan.
2. Implement Phase 0 + Phase 1 on the review branch.
3. Add tests and metrics.
4. Evaluate on archolith-bench.

This is a relatively low-risk, high-leverage improvement to context quality.