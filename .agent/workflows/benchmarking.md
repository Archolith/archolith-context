# Benchmarking & Tuning Workflow

> **Note (2026-06-06):** The standalone benchmark scripts (`scripts/benchmark.py`, `scripts/compare_experiments.py`, `scripts/scenarios/`) have been migrated to the **archolith-bench** project (`../archolith-bench/`). The benchmark CLI is now `archolith-bench proxy|filter|stack|audit|report`. The scenario format, experiment arms, and tuning workflows described below remain architecturally accurate but the script paths reference the old in-repo location. Use `archolith-bench` for all benchmark runs going forward.

## What this system does

The benchmark suite measures how well the archolith-proxy compresses conversation context while preserving the model's ability to recall facts from earlier turns. It sends the same multi-turn conversation through two paths:

1. **Passthrough** — request routes through the proxy unchanged (no context management), full history forwarded verbatim. Token counts recorded in the same trace store as the proxy path for accurate comparison. Uses `deepseek-passthrough/deepseek-v4-flash-passthrough` provider.
2. **Proxy** — archolith-proxy rewrites the conversation, replacing middle history with graph-assembled context + a coherence tail of recent messages

### Passthrough mode

The proxy detects any model name ending in `-passthrough`, strips the suffix before forwarding to DeepSeek, and skips all context management (no assembly, no injection, no graph writes, no extraction). The trace is still recorded (input_tokens, output_tokens, upstream_latency) so both sessions appear in the same trace explorer and the harness benchmark report.

This replaces the old approach of routing the "direct" session straight to the DeepSeek API, which made token comparison impossible since the trace store never saw those requests.

After specific turns, **fact probes** ask the model questions about earlier content and check whether expected keywords appear in the response. This gives two key metrics:

- **Savings ratio** — how much the proxy reduced the total token count (internal trace: input → rewritten)
- **Recall preservation** — what fraction of keywords the proxy path recalls vs direct

## How the proxy pipeline works (for tuning context)

```
User turn arrives
  → extract_facts(gpt-4.1-mini) from user msg + assistant response
    → caps: user 4000 chars, assistant 8000 chars, tools 4000 chars
    → outputs: facts[], files_touched[], decisions[], invalidated[], session_goal
  → deduplicate against existing facts (Jaccard 0.85 threshold)
  → store in session graph (LadybugDB or Neo4j)

Next request arrives
  → assemble_context(session_id, turn, token_budget)
    → cold start check (skip if too early)
    → optional: rewrite query for embedding retrieval
    → optional: compute query embedding
    → fetch all active facts (limit 200)
    → score facts by relevance:
      - WITH embeddings: similarity(40%) + recency(30%) + type+confidence(30%)
      - WITHOUT: type(40%) + confidence(30%) + recency(30%)
    → budget facts: fit highest-scored into token budget
    → N-1/N+1 context windowing (add facts from adjacent turns)
    → format: SESSION OVERVIEW (goal, files, decisions) + RELEVANT CONTEXT (fact list)
  → rewrite_messages()
    → merge graph context into system message
    → keep coherence tail (smart_tail preserves tool-call integrity)
    → discard middle messages
  → send rewritten payload upstream
```

## Known recall loss points

1. **Extraction truncation** — assistant responses capped at 8K chars. Long code reviews lose detail past this cap.
2. **Numeric value extraction** — probes test specific numbers (thresholds, config values). The extractor sometimes summarizes instead of extracting verbatim.
3. **Fact budget overhead** — hardcoded 200-token overhead for session overview is too low when there are many files/decisions, eating into fact budget.
4. **Coherence tail size** — `COHERENCE_TAIL_SIZE=3` means only last 3 messages survive. Everything else depends on extraction quality.
5. **Recency bias** — linear recency scoring penalizes early-turn facts. At turn 10, a turn-1 fact scores 0.1 recency vs 1.0 for turn 10.
6. **DeepSeek output collapse** — at later turns with tight budgets, DeepSeek produces 24-28 token stub responses through the proxy, suggesting the assembled context is too sparse or confusing for the model.

## Files

| File | Purpose |
|------|---------|
| `scripts/benchmark.py` | Main benchmark runner — scenarios, probes, checkpoints, experiments |
| `scripts/compare_experiments.py` | Side-by-side experiment comparison (config diffs + results) |
| `scripts/scenarios/*.json` | Scenario definitions (turns + fact probes with expected keywords) |
| `archolith_proxy/extractor/prompts.py` | Extraction prompt — what gpt-4.1-mini is told to extract |
| `archolith_proxy/extractor/client.py` | Extraction client — sends turns, parses JSON response |
| `archolith_proxy/extractor/dedup.py` | Fact deduplication (Jaccard similarity) |
| `archolith_proxy/assembler/context.py` | Context assembly — scoring, budgeting, formatting |
| `archolith_proxy/assembler/tail.py` | Smart coherence tail — preserves tool-call integrity |
| `archolith_proxy/proxy/rewrite.py` | Message rewriting — merges graph context + tail |
| `archolith_proxy/config.py` | Settings singleton (pydantic BaseSettings, env-driven) |

## Running benchmarks

```bash
# Single scenario, single budget
python scripts/benchmark.py --scenario scenarios/code_review.json --budget 4000

# All scenarios, multiple budgets (matrix run)
python scripts/benchmark.py --all --budgets 4000,8000,15000,32000

# Against a different upstream (e.g., DeepSeek on port 9801)
python scripts/benchmark.py --all --budgets 4000,8000 \
  --proxy http://localhost:9801/v1 \
  --direct https://api.deepseek.com/v1 \
  --model deepseek-chat \
  --api-key sk-xxx

# Resume from checkpoint after interruption
python scripts/benchmark.py --scenario scenarios/long_agent.json --budget 4000 --resume
```

## Running tuning experiments

Experiments are named benchmark runs that snapshot the proxy config alongside results, so you can compare different tuning configurations.

```bash
# 1. Run baseline with current config
python scripts/benchmark.py --experiment baseline \
  --scenario scenarios/code_review.json --budgets 4000,8000

# 2. Test a change — e.g., bigger coherence tail
python scripts/benchmark.py --experiment tail5 \
  --scenario scenarios/code_review.json --budgets 4000,8000 \
  --config '{"coherence_tail_size": 5}'

# 3. Compare results
python scripts/compare_experiments.py baseline tail5

# List all experiments
python scripts/compare_experiments.py --list
```

Each experiment saves to `scripts/results/experiments/<name>/`:
- `experiment.json` — metadata, proxy config snapshot, config overrides, results summary
- `benchmark_<scenario>_<budget>b.json` — per-run detailed results
- `transcripts/` — full conversation transcripts for manual review

The comparison script shows:
- **Config differences** — which settings changed between experiments
- **Per-scenario results** — savings ratio, recall, proxy tokens side-by-side
- **Averages** — overall savings/recall across all runs

## Tunable settings (via --config or /admin/config)

| Setting | Default | What it controls |
|---------|---------|-----------------|
| `context_token_budget` | 15000 | Max tokens for assembled context block |
| `coherence_tail_size` | 3 | Number of recent messages preserved verbatim |
| `max_tail_messages` | 20 | Cap on expanded tail (smart_tail won't exceed this) |
| `cold_start_turns` | 1 | Turns before assembly activates |
| `cold_start_token_threshold` | 200 | Token threshold for cold start bypass |
| `embedding_enabled` | true | Use embeddings for semantic fact retrieval |
| `query_rewrite_enabled` | true | Rewrite ambiguous queries before embedding |
| `compaction_enabled` | true | Enable context compaction |
| `session_recall_tool_enabled` | true | Inject session_recall tool |

## Tuning changes that need code edits (not just config)

| Change | File | What to modify |
|--------|------|---------------|
| Increase extraction input caps | `extractor/prompts.py:155-159` | `user_message[:4000]` → larger, `assistant_response[:8000]` → larger |
| Add numeric extraction emphasis | `extractor/prompts.py` | Add rule to SYSTEM_PROMPT about extracting specific values |
| Fix overhead calculation | `assembler/context.py:452` | Replace `fixed_overhead = 200` with computed overhead |
| Change recency curve | `assembler/context.py:101` | `recency = source_turn / max(turn_number, 1)` → logarithmic or capped |
| Change fact type priorities | `assembler/context.py:39-47` | `_FACT_TYPE_PRIORITY` dict |
| Change scoring weights | `assembler/context.py:109-112` | Similarity/recency/type weight ratios |
| Change dedup threshold | `extractor/dedup.py:12` | `DEFAULT_SIMILARITY_THRESHOLD = 0.85` |

## Scenarios

| Scenario | Turns | Probes | Tests |
|----------|-------|--------|-------|
| `code_review` | 10 | 4 | PR review with code blocks, file refs, line comments |
| `debugging` | 12 | 4 | Bug investigation with tracebacks, root cause, fixes |
| `long_agent` | 30 | 5 | Extended session — tests recall degradation over time |
| `ruler_recall` | 15 | 8 | Dense facts with specific numbers — hardest recall test |
| `taskflow` | 16 | 4 | Feature implementation across multiple components |

## Interpreting results

- **Savings ratio** in the benchmark is computed from the proxy trace (internal compression: `input_tokens - rewritten_tokens`), NOT from comparing direct vs proxy actual token counts. The proxy can send more tokens than direct (because it injects assembled context) while still showing internal savings.
- **Recall preservation > 100%** means the proxy path recalls MORE keywords than direct. This happens when the proxy's assembled context surfaces facts that the model would have lost in long raw history.
- **Recall < 50%** at tight budgets (4K) is expected — aggressive compression necessarily loses detail. The question is whether 8K or 15K recovers enough.
- **Output collapse** (proxy responses dropping to <50 tokens) indicates the model is confused by the assembled context, not just missing facts. Check the transcript to see what context was sent.

## Tuning experiments (2026-06-04)

Four config variants from the RTK curator tuning plan (archolith-filter-curator-tuning-plan.md).
These should be run after Step 1 instrumentation is live so the dashboard shows RTK stats and
curator skip reasons alongside savings metrics.

### Variant A — lower AGENT_SOLO_MIN_INPUT_TOKENS (active in .env)

Drop the threshold from 8000 → 3000 so agent-solo compression activates from turn 2 onward
in nearly every coding session.

```bash
# Baseline (8000 default)
python scripts/benchmark.py --experiment solo_threshold_8k \
  --scenario scenarios/long_agent.json --budgets 8000,15000 \
  --config '{"agent_solo_min_input_tokens": 8000}'

# Active config (3000 — already set in .env)
python scripts/benchmark.py --experiment solo_threshold_3k \
  --scenario scenarios/long_agent.json --budgets 8000,15000 \
  --config '{"agent_solo_min_input_tokens": 3000}'

python scripts/compare_experiments.py solo_threshold_8k solo_threshold_3k
```

Watch for: savings ratio increase, recall preservation staying >0.85, no output collapse.

### Variant B — stale briefing tolerance

The inline curator pass (max_iterations=2, 3s cap) is already aggressive. The lever here
is `BRIEFING_MAX_STALENESS`: how many turns old a briefing may be before the inline pass
ignores it and falls back to cold assembler. Loosening this (2 → 4) reduces background-pass
pressure at the cost of potentially serving stale context.

```bash
python scripts/benchmark.py --experiment briefing_staleness_2 \
  --scenario scenarios/taskflow.json --budgets 8000,15000 \
  --config '{"briefing_max_staleness": 2}'

python scripts/benchmark.py --experiment briefing_staleness_4 \
  --scenario scenarios/taskflow.json --budgets 8000,15000 \
  --config '{"briefing_max_staleness": 4}'

python scripts/compare_experiments.py briefing_staleness_2 briefing_staleness_4
```

Watch for: assembly_latency_ms changes, curator assembly_mode distribution (briefing vs curator).

### Variant C — synthetic tools off

SYNTHETIC_TOOLS_ENABLED injects `session_recall` and related synthetic tools into every
turn. Turning this off removes ~300-500 tokens of overhead per turn. Check whether recall
quality suffers when the model can't invoke session_recall explicitly.

```bash
python scripts/benchmark.py --experiment synthetic_on \
  --scenario scenarios/ruler_recall.json --budgets 8000,15000 \
  --config '{"synthetic_tools_enabled": true}'

python scripts/benchmark.py --experiment synthetic_off \
  --scenario scenarios/ruler_recall.json --budgets 8000,15000 \
  --config '{"synthetic_tools_enabled": false}'

python scripts/compare_experiments.py synthetic_on synthetic_off
```

Watch for: savings ratio up (less overhead), recall drop if model relied on session_recall
invocations to surface forgotten facts.

### Variant D — background pass vs no background pass

Background pass builds a SessionBriefing asynchronously after each user turn, allowing the
inline curator to run in 2 iterations instead of 4-6. Disabling it falls back to cold
assembly every turn (higher latency, potentially richer context).

```bash
python scripts/benchmark.py --experiment bg_pass_on \
  --scenario scenarios/long_agent.json --budgets 8000,15000 \
  --config '{"background_pass_enabled": true}'

python scripts/benchmark.py --experiment bg_pass_off \
  --scenario scenarios/long_agent.json --budgets 8000,15000 \
  --config '{"background_pass_enabled": false}'

python scripts/compare_experiments.py bg_pass_on bg_pass_off
```

Watch for: assembly_latency_ms (should drop with bg_pass_on), recall difference, savings ratio.
Check dashboard for `assembly_mode` distribution — briefing vs curator vs passthrough.

### Reading results

After running experiments, check the dashboard trace explorer alongside the compare output:
- `rtk_available` badge confirms RTK filter is active
- `curator_skip_reason` explains cold_start / disabled / no_result on passthrough turns
- `rtk_chars_saved` shows per-turn filter savings alongside the benchmark savings_ratio

## Step 0 additions (2026-06-05)

Three additions from the RTK/curator tuning plan's Step 0 (extend the existing harness; do not
rebuild it).

### Edit-fidelity probes

`FactProbe` measures whether the model *recalls* a keyword. `EditProbe` measures whether the
model can still produce the *correct change* under proxy-compressed context — the fidelity
signal behind the plan's hard fidelity gate. Add an `edit_probes` array to any scenario JSON:

```json
"edit_probes": [
  {
    "after_turn": 6,
    "instruction": "Apply the fix we discussed to parse_config() and show the edited function.",
    "required_fragments": ["def parse_config", "timeout = 30"],
    "forbidden_fragments": ["timeout = 10"]
  }
]
```

Scoring (`score_edit_probe`, pure/deterministic): fidelity = fraction of `required_fragments`
present, but **0.0 if any `forbidden_fragment` appears** (a stale/wrong edit fails outright).
The run reports `avg_direct_fidelity`, `avg_proxy_fidelity`, `fidelity_preservation`, and a
count of `proxy_forbidden_hits`. Use `fidelity_preservation` (proxy/direct) as the gate: a tuning
change that drops it below 1.0 is a fidelity regression regardless of token savings.

### Read-file redundancy analyzer

`scripts/redundancy.py` classifies the tokens spent on file-read tool results in a captured
session into exact-dup / superseded-by-full-write / live buckets. Run it on a captured session
JSON to SIZE whether RTK superseded-read collapse (Step 5-B) or curator condensing (Step 4-C)
are worth building before writing any of that code:

```bash
python scripts/redundancy.py path/to/session.json
```

If the corpus is mostly exact-dup tokens, the existing dedup (agent_solo Strategy B) already
closes the gap and superseded-collapse is not worth the risk. Partial edits deliberately do NOT
count as superseding (only full-file writes do).

### Determinism decision: variance-based, not byte-replay

The harness makes live upstream calls at `temperature=0.3`, so reruns are **not** byte-identical.
Rather than build a record/replay cache, the chosen approach is **variance-based**: run each
config as a named experiment **N times** (N>=3) and compare **medians and spread** via
`compare_experiments.py`, not single-run point values. A tuning change counts only if the median
moves beyond the run-to-run spread of the baseline. This keeps the harness honest about its live
nature instead of pretending to a determinism it does not have.
