# Goal-Drift Detection → Aggressive Re-weighting

**Status:** Proposed  
**Author:** Arena Agent  
**Date:** 2026-07-19  
**Related Review Item:** Goal-drift detection (Medium effort, Medium-High value)  
**Goal:** Detect when a long-running coding session has significantly drifted from its original goal and apply aggressive re-weighting to reduce the influence of stale, low-relevance facts.

---

## Problem Statement

The session goal is typically captured once at the beginning of a conversation. Over the course of a long session (often 30–100+ turns), both the user and the agent frequently shift focus to new sub-tasks, major refactors, or entirely different features.

**Current limitations:**
- Facts, decisions, and file states continue to be scored and ranked primarily against the **original** session goal.
- High-relevance facts from early in the session remain heavily weighted long after they have become irrelevant.
- This leads to:
  - Polluted context with outdated information
  - Wasted token budget
  - Reduced effectiveness of both the deterministic assembler and the curator

**Concrete example:**
- Turn 1 goal: “Implement user authentication with JWT”
- By turn 40: The agent is now building an unrelated admin dashboard
- Facts about password hashing, token refresh logic, and session middleware are still being surfaced because they match the original goal embedding

---

## Goals

| Goal | Target | Measurement |
|------|--------|-------------|
| Reliably detect meaningful goal drift | ≥ 75–80% of clear pivot turns flagged | Manual review + benchmark scenarios |
| Improve post-drift context quality | Higher fact relevance after drift | archolith-bench context_quality_ab |
| Reduce stale fact inclusion | Measurable drop in pre-drift facts in assembled context | Trace analysis |
| Low performance overhead | Detection < 5ms per turn | Instrumentation |
| Configurable behavior | Operators can tune sensitivity | New config knobs |

---

## Proposed Approach

Add a **Goal Drift Detector** that runs before fact scoring and assembly.

### Core Idea

1. Store an embedding (or strong fingerprint) of the original session goal.
2. On user turns (or periodically), compute a **drift score** between:
   - The original goal embedding
   - A sliding window of recent user messages
3. When drift exceeds a threshold:
   - Mark the session as drifted
   - Apply aggressive down-weighting to facts created before the drift point
   - Record drift metadata in traces

---

## Detection Strategy

### Primary Method: Embedding Similarity (Recommended)

- On session creation, compute and store an embedding of the initial `session_goal`.
- On each user turn (or every N turns), compute the cosine similarity between:
  - Original goal embedding
  - Embedding of the last K user messages (e.g., last 3–5 turns)
- If similarity falls below `goal_drift_similarity_threshold` → drift detected.

**Advantages:**
- Leverages existing embedding infrastructure (`search_facts_semantic`)
- Robust to paraphrasing and minor rewording

### Secondary / Fast-Path Signals

- Explicit reset language (“start over”, “new task”, “forget the previous goal”)
- Sudden major shift in touched files unrelated to the original goal
- Long period with no facts related to the original goal

These can be used to increase confidence or as a cheap pre-filter.

---

## Re-weighting Strategy (Aggressive)

When drift is detected:

1. **Fact Scoring Penalty**
   - Pre-drift facts receive a strong multiplier (e.g., `score *= 0.25–0.35`)
   - Recent facts (last 5–10 turns) can receive a small boost
   - The penalty can be made time-decaying (older = harsher)

2. **Assembler / Curator Awareness**
   - Pass a `goal_drifted` flag and `drift_turn` to the deterministic assembler and curator
   - Optionally include a short “Goal Drift” section in the assembled context so the agent is aware the original goal is no longer dominant

3. **Optional Aggressive Behaviors** (future)
   - Drop facts below a certain age + relevance threshold entirely
   - Force a fresh curator pass focused on recent turns

---

## Implementation Phases

### Phase 0 – Detection Only (Low Risk)
- Store goal embedding on session creation
- Implement `detect_goal_drift()` function
- Add config flags:
  - `goal_drift_detection_enabled`
  - `goal_drift_similarity_threshold`
  - `goal_drift_lookback_turns`
- Log drift events (no re-weighting yet)

### Phase 1 – Basic Re-weighting
- When drift is detected, apply a simple penalty multiplier during fact scoring
- Record in `TurnTrace`:
  - `goal_drift_detected`
  - `goal_drift_similarity`
  - `drift_turn`

### Phase 2 – Full Integration
- Pass drift information to the deterministic assembler and curator
- Add `goal_drifted` flag and context note in assembled output
- Add metrics:
  - `goal_drift_detections`
  - `goal_drift_fact_penalties_applied`

### Phase 3 – Advanced (Optional)
- Time-decaying penalty
- Support for multiple drift points
- User/agent-triggered `reset_goal` signal

---

## Technical Details

### New Graph Fields (Session node)

- `original_goal_embedding`: vector
- `drift_detected`: boolean
- `drift_turn`: integer

### New Trace Fields (`TurnTrace`)

- `goal_drift_detected`: bool
- `goal_drift_similarity`: float
- `drift_turn`: int
- `pre_drift_facts_downweighted`: int

### New Config Settings

```python
goal_drift_detection_enabled: bool = False
goal_drift_similarity_threshold: float = 0.40
goal_drift_penalty_multiplier: float = 0.30
goal_drift_lookback_turns: int = 5
```

---

## Risks & Trade-offs

| Risk | Mitigation |
|------|------------|
| False positives (unnecessary down-weighting) | Start conservative; make threshold tunable |
| False negatives (missed drift) | Combine embedding + rule-based signals |
| Performance cost | Cache goal embedding; only check on user turns |
| Over-penalization of useful old facts | Keep a minimum relevance floor or recency bonus |

---

## Success Metrics

- `goal_drift_detections`
- `goal_drift_fact_penalties_applied`
- Improvement in post-drift context quality scores
- Reduction in stale pre-drift facts appearing in assembled context

---

## Open Questions

1. Should drift detection run every turn or only every N turns?
2. Should we distinguish between “goal evolution” (legitimate change) and “drift” (accidental divergence)?
3. Do we want to expose a `reset_goal` tool or signal?

---

## Relationship to Existing Work

- Complements **Prompt Cache Stability** (drifted sessions benefit from stable recent context).
- Works synergistically with **Adaptive Tail Sizing** (pivot turns often coincide with goal drift).
- Significantly improves the value of the **deterministic assembler** in long sessions.

---

## Next Steps (if approved)

1. Create this plan as an official document.
2. Implement Phase 0 + Phase 1 on the review branch.
3. Add tests and metrics.
4. Evaluate on long-running archolith-bench scenarios.

This is a high-leverage improvement for exactly the kind of long, evolving sessions the proxy is designed to support.