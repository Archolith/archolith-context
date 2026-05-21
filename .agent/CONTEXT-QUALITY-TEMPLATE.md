# Context Quality Scorecard — <title>

**Date:** YYYY-MM-DD
**Auditor:** <who>
**Session ID:** <proxy session ID>
**Benchmark:** <link to benchmark audit>
**Commit:** <HEAD>

---

## Methodology

Context quality measures whether the engine surfaces the **right knowledge** — not just fewer tokens. Two independent scores:

- **Recall** — does the assembled context include facts the LLM needs?
- **Precision** — is the assembled context free of noise?

### How to Score a Turn

For each sampled turn:

1. **Read the user message.** Identify what prior context it requires (explicit references like "the Task entity we defined" and implicit ones like "update the worker loop" which requires knowing the current worker code).

2. **Build the reference set.** From the full conversation history, list every fact, decision, entity, and code artifact the LLM needs to answer correctly. These are the **should-know** items.

3. **Read the assembled context.** From the trace (`/trace/turns/{id}`), examine `rewritten_messages` — specifically the system message containing the graph context block. List every fact in the `=== RELEVANT CONTEXT ===` section. These are the **does-know** items.

4. **Score recall.** `should-know items found in does-know / total should-know items`.

5. **Score precision.** `does-know items relevant to this turn / total does-know items`.

6. **Compare responses.** Read both the direct and proxy LLM responses. Mark whether the proxy response demonstrates equivalent knowledge, misses something, or hallucinates something the context introduced.

---

## Session Summary

| Metric | Value |
|--------|-------|
| Total turns | |
| Turns sampled | |
| Total facts in graph (end of session) | |
| Total decisions in graph | |
| Mean assembled context size (tokens) | |

---

## Turn Samples

Sample at least 5 turns: 1 early, 2 mid, 2 late. Include any turn where the proxy response diverged from direct.

### Turn <N>: <user message summary>

**User message requires:** (the reference / should-know set)
1. <fact or entity the LLM needs>
2. <fact or entity the LLM needs>
3. ...

**Assembled context contains:** (from trace rewritten_messages)
- Total facts in context: <N>
- Relevant to this query: <N>
- Irrelevant to this query: <N>
- Missing (should-know but absent): <N>

| Should-Know Item | In Context? | Notes |
|------------------|-------------|-------|
| <item> | Yes / No | |
| <item> | Yes / No | |

**Scores:**
- Recall: ___% (<found> / <should-know>)
- Precision: ___% (<relevant> / <total in context>)

**Response comparison:**
- Direct response: <1-2 sentence summary of key content>
- Proxy response: <1-2 sentence summary of key content>
- Knowledge gap: <none / missed X / hallucinated Y>
- Quality verdict: Equivalent / Proxy better / Proxy worse

---

## Aggregate Scores

| Metric | Turn ? | Turn ? | Turn ? | Turn ? | Turn ? | Mean |
|--------|--------|--------|--------|--------|--------|------|
| Recall | | | | | | |
| Precision | | | | | | |
| Response equiv. | | | | | | |

### Overall Context Quality Score

```
Context Quality = (Recall * 0.50) + (Precision * 0.25) + (Response Equivalence * 0.25)
```

| Component | Weight | Score | Weighted |
|-----------|--------|-------|----------|
| Mean Recall | 50% | | |
| Mean Precision | 25% | | |
| Response Equivalence Rate | 25% | | |
| **Context Quality Score** | | | **___/100** |

---

## Failure Analysis

For each turn where context quality was poor, diagnose why.

### Pattern: <failure pattern name>

- **Turns affected:** <list>
- **Symptom:** <what went wrong — missing fact, wrong fact, stale fact>
- **Root cause:** <why — extraction missed it, embedding scored it low, budget excluded it, superseded incorrectly, dedup removed it>
- **Fix category:** Extraction prompt / Relevance scoring / Budget tuning / Dedup logic / Invalidation logic

---

## Extraction Quality

How well does the extractor (gpt-4.1-mini) capture knowledge from each turn?

### Extraction Audit (sample 3-5 turns)

#### Turn <N>

**Conversation content:** <1-2 sentence summary of what was said>

**Extracted facts:**
1. <fact> — Correct / Incorrect / Incomplete / Redundant
2. <fact> — Correct / Incorrect / Incomplete / Redundant

**Missing extractions:** (things said in the turn that should have been extracted)
1. <missed item>

**Extraction scores:**
- Completeness: ___% (extracted / should-have-extracted)
- Accuracy: ___% (correct / total extracted)
- Redundancy rate: ___% (redundant / total extracted)

### Extraction Summary

| Metric | Value | Target |
|--------|-------|--------|
| Mean completeness | | >= 80% |
| Mean accuracy | | >= 90% |
| Mean redundancy rate | | <= 15% |

---

## Relevance Scoring Analysis

How well does the relevance scorer rank facts for each query?

### Turn <N>: Top-5 facts by relevance score

| Rank | Fact (truncated) | Relevance Score | Actually Relevant? |
|------|------------------|-----------------|--------------------|
| 1 | | | Yes / No |
| 2 | | | Yes / No |
| 3 | | | Yes / No |
| 4 | | | Yes / No |
| 5 | | | Yes / No |

**Ranking quality:** <good / acceptable / poor>
**Notes:** <e.g. "embedding similarity correctly boosted the retry logic fact">

---

## Decision & Goal Tracking

| Metric | Value | Notes |
|--------|-------|-------|
| Decisions made in conversation | | Count from reading the full history |
| Decisions captured in graph | | From `/trace/graph/{session}/decisions` |
| Decision recall | | captured / made |
| Goal accuracy | | Does the session goal reflect the actual conversation topic? |

---

## Recommendations

### Extraction

1. <recommendation — e.g. "extractor misses code structure facts; add example to extraction prompt">

### Relevance Scoring

1. <recommendation — e.g. "recency weight too high; older architectural decisions scored below recent observations">

### Budget & Assembly

1. <recommendation — e.g. "15K budget is never reached; could tighten to 8K to force better prioritization">

### Dedup & Invalidation

1. <recommendation — e.g. "duplicate facts about the Task model across turns 1, 3, 4; dedup not catching paraphrases">

---

## Comparison to Prior Scorecard

| Metric | Prior (<date>) | Current | Delta |
|--------|----------------|---------|-------|
| Mean Recall | | | |
| Mean Precision | | | |
| Response Equivalence | | | |
| Context Quality Score | | | |
| Extraction Completeness | | | |

## Conclusion

1-3 sentences: is the engine surfacing the right context? What is the biggest quality gap?

---

## Notes

- This scorecard requires manual judgment — there is no automated way to determine whether a fact is "relevant to this query." The auditor reads the user message, identifies what context is needed, and checks whether the assembled context provides it.
- Use the `/trace/turns/{id}` endpoint to get `rewritten_messages` (what the LLM actually saw) and `original_messages` (what the client sent).
- Use `/trace/graph/{session}/facts` to see all facts with their types, confidence, and source turns.
- Use `/trace/qa/extract` to re-run extraction on specific turns without affecting the graph.
- The extraction audit is independent of the recall audit. Extraction quality feeds recall quality, but they measure different things: extraction = "did we capture it?" vs recall = "did we surface it when needed?"
- Score at least 5 turns for statistical meaning. If time-constrained, prioritize turns where the user explicitly references prior context ("the entity we defined", "remind me", "update the X we discussed").
