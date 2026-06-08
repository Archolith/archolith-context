# Feature Rollout Matrix

A guardrail for the proxy's optional, quality-affecting feature flags. The proxy can
turn on several behaviours that *might* help but also carry cost or risk (embeddings,
query rewrite, the recall tool, compaction, more aggressive assembly thresholds).
This document forces each one to declare, **before** it is enabled:

- what it does and why we think it helps (the bet)
- what it costs (money, latency, complexity)
- how it can fail
- which benchmark sessions exercise it
- the **enablement gate** — the measurable bar that must be cleared to turn it on
- the **rollback rule** — the condition that turns it back off
- its current state

This is a hand-maintained governance doc, not enforced code. The enablement gates and
rollback rules only have teeth when measured — wire them to benchmark/eval results
before flipping a flag. Default state for every track below is **disabled**.

Source: lifted from `feat/evaluation-and-rollout:eval/feature_matrix.py`. Config-flag
names verified against the current `archolith_proxy/config.py`.

---

## Embedding-driven Retrieval — `EMBEDDING_ENABLED`

- **What:** Use `text-embedding-3-small` cosine-similarity fact scoring instead of priority-only ranking.
- **Bet:** Improves retrieval relevance by 20%+ on ambiguous-reference sessions, ~50ms/query cost.
- **Cost:** One embedding API call/turn (~$0.00002/1K tokens); 50-100ms added per request.
- **Failure modes:** (1) embedding API downtime -> fall back to priority scoring; (2) poor model for code queries -> worse relevance; (3) cache poisoning -> stale embeddings for dissimilar queries.
- **Benchmark sessions:** ambiguous-reference-001, tool-heavy-001, file-search-heavy-001
- **Enablement gate:** Retrieval relevance on ambiguous sessions >= 60% with embeddings (vs baseline). No regression on short-session safety.
- **Rollback rule:** If relevance drops below priority-only baseline on any category, or embedding latency > 200ms p95.
- **State:** disabled

## Query Rewrite for Ambiguous Messages — `QUERY_REWRITE_ENABLED`

- **What:** Resolve pronouns / vague references in user messages before embedding lookup, via a cheap model call.
- **Bet:** Resolves 80%+ of pronoun references in ambiguous sessions, improving recall without significant cost.
- **Cost:** One gpt-4.1-mini call/turn on ambiguous messages (~$0.00015); 200-500ms added. Only fires when `needs_rewrite()` is true.
- **Failure modes:** (1) rewrite changes the message's meaning -> wrong facts retrieved; (2) latency spike when many messages need rewriting; (3) rewrite loop.
- **Benchmark sessions:** ambiguous-reference-001
- **Enablement gate:** Ambiguous-reference continuity score >= 0.90 with rewrite (vs baseline). No meaning-change failures.
- **Rollback rule:** If any rewrite changes the user message's meaning (human review), or p95 latency exceeds the assembly budget.
- **State:** disabled

## Session Recall as Proxy-Intercepted Tool — `SESSION_RECALL_TOOL_ENABLED`

- **What:** Inject a `__context_engine_recall` tool so the model can explicitly request context from the session graph.
- **Bet:** Gives the model an escape hatch when implicit context is insufficient, cutting critical context misses by 50%+.
- **Cost:** No extra upstream calls (intercepted before upstream). Complexity: streaming interception + tool injection. Latency: 0ms normally, 200-500ms when recall fires.
- **Failure modes:** (1) model ignores the tool; (2) model over-uses it, wasting tokens; (3) streaming interception fails -> double response to client.
- **Benchmark sessions:** long-implementation-001, ambiguous-reference-001, recovery-failure-001
- **Enablement gate:** Continuity score with recall >= 0.95 on long sessions (vs baseline). No regression on tool-heavy sessions.
- **Rollback rule:** If interception causes any streaming failures, or model recall usage exceeds 30% of turns (over-reliance).
- **State:** disabled

## Context Overflow Compaction — `COMPACTION_ENABLED`

- **What:** When assembled context exceeds the token budget, progressively compact (summarize oldest facts, reduce detail) as a fallback.
- **Bet:** Prevents context-overflow errors on extremely long sessions without losing all context. Better than hard truncation.
- **Cost:** One gpt-4.1-mini summarization call when compaction triggers (rare). No latency impact on normal sessions.
- **Failure modes:** (1) summarization loses critical details; (2) triggers too eagerly, reducing quality; (3) compaction loop (compacted context still overflows).
- **Benchmark sessions:** long-implementation-001, tool-heavy-001
- **Enablement gate:** No context-overflow errors on any golden session with compaction. Fact preservation >= 70% of graph-only mode.
- **Rollback rule:** If fact preservation drops below 50%, or compaction triggers on more than 10% of normal-length sessions.
- **State:** disabled

## More Aggressive Assembly Thresholds — `ASSEMBLY_MIN_SAVINGS_RATIO` + `ASSEMBLY_MIN_INPUT_TOKENS`

- **What:** Lower the savings-ratio gate and input-token threshold to start rewriting earlier in sessions.
- **Bet:** Earlier rewriting saves tokens on medium sessions (currently passthrough), at an acceptable continuity trade-off.
- **Cost:** No extra upstream calls; more sessions rewritten -> more extraction calls but similar total cost. Risk: rewriting medium sessions that do not benefit enough.
- **Failure modes:** (1) medium sessions lose continuity for marginal savings; (2) short sessions caught by the lower threshold; (3) savings do not justify context loss.
- **Benchmark sessions:** medium-debugging-001, short-exploration-001
- **Enablement gate:** Medium-session token savings >= 15% with no continuity regression. Short sessions still 100% passthrough.
- **Rollback rule:** If any short session (< 20K tokens) gets rewritten, or medium-session continuity drops below 90%.
- **State:** disabled
- **Note:** This gate now keys on the structural token estimate (`archolith_proxy/token_accounting/`), which counts tool schemas the old crude estimate missed — so "input tokens" here means the true request size, not just message content.
