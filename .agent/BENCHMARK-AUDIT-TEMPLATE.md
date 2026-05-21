# Benchmark Audit — <title>

**Date:** YYYY-MM-DD
**Operator:** <who ran the benchmark>
**Commit:** <HEAD at time of test>
**Branch:** <branch name>

---

## System Under Test

| Component | Value |
|-----------|-------|
| Proxy version | `<from /health>` |
| Graph backend | `neo4j` / `ladybug` |
| Upstream LLM | `<model name>` |
| Upstream URL | `<base URL>` |
| Extractor model | `<model name>` |
| Embedding model | `<model name>` |

## Configuration Snapshot

Capture every tuning knob that affects assembly, extraction, or pipeline behavior.

| Setting | Value | Default | Notes |
|---------|-------|---------|-------|
| `COLD_START_TURNS` | | 3 | |
| `COLD_START_TOKEN_THRESHOLD` | | 20000 | |
| `ASSEMBLY_MIN_INPUT_TOKENS` | | 50000 | |
| `ASSEMBLY_MIN_SAVINGS_RATIO` | | 0.20 | |
| `CONTEXT_TOKEN_BUDGET` | | 15000 | |
| `COHERENCE_TAIL_SIZE` | | 3 | |
| `MAX_TAIL_MESSAGES` | | 20 | |
| `EMBEDDING_ENABLED` | | false | |
| `QUERY_REWRITE_ENABLED` | | false | |
| `COMPACTION_ENABLED` | | false | |
| `SESSION_RECALL_TOOL_ENABLED` | | false | |
| `PROMOTION_ENABLED` | | false | |

## Scenario

Describe the benchmark conversation scenario.

- **Type:** <coding / Q&A / debug / mixed>
- **Turns:** <number>
- **Topic:** <1-2 sentence description>
- **Complexity profile:** <e.g. "builds a FastAPI service over 16 turns, each referencing prior context">
- **Script path:** `<relative path to benchmark script>`
- **Results file:** `<relative path to JSON output>`

## Results

### Per-Turn Data

| Turn | Direct In | Proxy In | Rewritten | Savings | Ratio | Assembly Mode | Facts Stored | Direct ms | Proxy ms |
|------|-----------|----------|-----------|---------|-------|---------------|-------------|-----------|----------|
| 1 | | | | | | | | | |

### Aggregates

| Metric | Value |
|--------|-------|
| Total direct input tokens | |
| Total proxy savings | |
| Overall savings ratio | |
| Peak savings ratio (single turn) | |
| Crossover turn (first positive savings) | |
| Steady-state savings range | |
| Total facts extracted | |
| Total decisions tracked | |
| Final graph size (facts) | |

### Latency

| Metric | Direct | Proxy | Delta |
|--------|--------|-------|-------|
| Mean response time (ms) | | | |
| P50 response time (ms) | | | |
| P95 response time (ms) | | | |
| Mean assembly latency (ms) | | | |

## Graph State

Post-benchmark graph inspection via `/trace/graph/{session_id}/*`.

- **Session ID:** `<id>`
- **Active facts:** <count>
- **Invalidated facts:** <count>
- **Supersession chains:** <count>
- **Touched files:** <count>
- **Decisions:** <count>
- **Recall events:** <count>

## Pipeline Feature Coverage

Check each feature that fired during the benchmark.

- [ ] Session creation (fingerprint-based)
- [ ] Cold start passthrough
- [ ] Query rewriting
- [ ] Embedding computation (query + fact)
- [ ] Context assembly (graph mode)
- [ ] Coherence tail (last N messages)
- [ ] Savings-ratio gate
- [ ] Token-minimum gate
- [ ] Context-overflow compaction
- [ ] Recall tool injection
- [ ] Recall tool interception + re-send
- [ ] Fact extraction (post-response)
- [ ] Fact deduplication
- [ ] Fact invalidation / supersession
- [ ] Decision extraction
- [ ] File-touch tracking
- [ ] Goal update
- [ ] Promotion to long-term memory

## Response Quality Assessment

Subjective comparison of proxy vs direct responses on key turns.

### Turn <N>: <short description>

- **Direct response:** <1-2 sentence summary>
- **Proxy response:** <1-2 sentence summary>
- **Quality delta:** <proxy better / equivalent / worse>
- **Notes:** <what context did the proxy have or miss?>

Repeat for 3-5 representative turns, including at least:
1. An early turn (before assembly fires)
2. A mid-conversation turn that references prior context
3. A late turn where savings are highest

## Findings

### Observations

1. <observation with data reference>

### Issues Found

Severity: P1 (broken) / P2 (material gap) / P3 (minor)

1. **[P?]** <issue> — <evidence>

### Tuning Recommendations

1. <parameter> — <current value> — <recommended value> — <rationale>

## Comparison to Prior Audits

If previous benchmark audits exist, compare key metrics.

| Metric | This Run | Prior (<date>) | Delta |
|--------|----------|----------------|-------|
| Steady-state savings | | | |
| Crossover turn | | | |
| Extraction quality | | | |
| Assembly latency | | | |

## Conclusion

1-3 sentences: is the system performing as designed? What is the most impactful next improvement?

---

## Notes

- Always run against a **clean database** to avoid session bleed between audits.
- Kill the proxy cleanly or wipe the LadybugDB WAL before restarting — hard kills can corrupt the WAL.
- Never commit `.env` — it contains API keys.
- Store results JSON alongside the audit in `scripts/` or `.agent/audits/`.
- The benchmark script's trace parsing reads the in-memory trace store, which is per-process. A proxy restart clears all trace data.
- Token counts from the benchmark script use char/4 estimation. Server-side logs use the same estimator. For exact counts, check `usage` in the upstream response JSON.
