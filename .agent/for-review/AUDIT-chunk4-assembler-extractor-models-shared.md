# Chunk 4 Audit: assembler/, extractor/, models/, shared/

**Auditor**: opencode (z-ai/glm-5.1)
**Date**: 2026-06-07
**Scope**: `archolith_proxy/assembler/`, `archolith_proxy/extractor/`, `archolith_proxy/models/`, `archolith_proxy/shared/`
**Prior Audit Status**: Memory recall found "Chunk4 extractor bugs" referenced in prior remediation-plan review (uuid `5cf4c336`), but no full chunk-4 audit artifact exists. This is the first comprehensive audit.

---

## Import DAG

```
shared.text_utils ──→ extractor.dedup ──→ extractor.client
                                        ──→ extractor.registry
                  ──→ shared.__init__

models.graph_nodes ──→ models.enums
                   ──→ models.__init__ ──→ models.dtos
                                        ──→ assembler.context

models.dtos ──→ extractor.client
           ──→ assembler.context

extractor.base ──→ extractor.registry ──→ extractor.__init__
               ──→ extractor.extractors.*
               ──→ extractor.client

extractor.prompts ──→ extractor.client
                 ──→ extractor.extractors.bash
                 ──→ extractor.extractors.web_fetch
                 ──→ extractor.extractors.default

extractor.embeddings ──→ assembler.context (cross-layer: assembler → extractor)

config ──→ assembler.context, assembler.compaction, assembler.query_rewrite
       ──→ extractor.client, extractor.embeddings
       ──→ extractor.extractors.bash, extractor.extractors.web_fetch, extractor.extractors.default

graph.backend ──→ assembler.context
```

### Cycle Check

**No import cycles detected.** The DAG is a clean hierarchy:
- `shared` → leaf (no internal deps beyond stdlib/re)
- `models` → leaf (pydantic only)
- `extractor` → depends on `shared`, `models`, `config`
- `assembler` → depends on `models`, `extractor.embeddings`, `config`, `graph.backend`

**Cross-layer concern**: `assembler/context.py:637` imports from `extractor.embeddings` — the assembler layer reaches down into the extractor layer. This is a known pragmatic shortcut (embedding computation lives in extractor but is needed at assembly time). Not a cycle, but a coupling violation worth noting.

---

## Findings

### F-01: Extraction Race Condition — TOCTOU in dedup+store path
**File**: `extractor/client.py:313-384`
**Severity**: **Critical**
**Category**: Correctness / Concurrency

`extract_facts_per_tool()` runs per-tool extractors concurrently via `asyncio.gather()` (line 313), then merges results and calls `deduplicate_facts()` against `all_facts` (line 383). However, the dedup check happens in-process against the *current turn's* partial results — it does NOT check against facts already stored in the graph by a concurrent or prior turn's extraction.

The real race: two concurrent proxy turns for the same session could both pass dedup (neither sees the other's in-flight facts), then both write to the graph, producing duplicate facts that the Jaccard dedup should have caught. This is the **known extraction race condition** referenced in the task.

**Impact**: Duplicate facts inflate the knowledge graph, degrading retrieval quality and wasting token budget on redundant context in assembly.

**Remediation**: (a) Add graph-level dedup as a post-write consistency check, or (b) use a per-session lock/semaphore around the extract→dedup→store pipeline, or (c) accept eventual consistency and add a periodic graph-level dedup sweep.

---

### F-02: Dedup Ratio Not Monitored or Alerted
**File**: `extractor/dedup.py:96-123`
**Severity**: **High**
**Category**: Observability / Known Concern

`deduplicate_facts()` logs `facts_deduplicated` at INFO level when duplicates are found, but:
1. The dedup ratio (skipped / total) is computed but **never emitted as a structured metric** (counter, gauge, or histogram).
2. There is **no alerting threshold** — if dedup ratio spikes (e.g. extractor hallucinating the same fact repeatedly), there's no signal.
3. The `compress_facts_batch()` compression_ratio is tracked in `AssembledContext.compression_ratio` and logged, but the dedup ratio is only in log lines.

This is the **known dedup-ratio-monitoring concern** from the task.

**Impact**: Silent degradation — if the extractor goes haywire and produces near-duplicate facts that barely pass the Jaccard 0.85 threshold, operators have no way to detect it without log mining.

**Remediation**: Emit `dedup_ratio` as a structured metric (e.g. `dedup_skipped / dedup_total`) alongside the log. Add a Prometheus counter or at minimum a structlog field that can be alerted on.

---

### F-03: Module-level mutable global — embedding cache has no concurrency protection
**File**: `assembler/context.py:38-39`
**Severity**: **Medium**
**Category**: Thread Safety

`_embedding_cache` is a module-level `dict` with TTL/size-based eviction (`_evict_embedding_cache()`). In an asyncio context, dict mutations are safe within a single event loop (no preemptive threads). However:
1. If the proxy ever runs with thread executors or multiple event loops, concurrent dict mutation is unsafe.
2. `_evict_embedding_cache()` iterates the dict and deletes keys — safe in CPython 3.11+ due to GIL, but fragile under alternative runtimes (free-threaded Python 3.13+).

**Impact**: Low in current single-event-loop deployment. Medium if deployment model changes.

**Remediation**: Use `asyncio.Lock` around cache access, or switch to a thread-safe `OrderedDict` / LRU cache.

---

### F-04: tiktoken loaded lazily on every call — repeated encoding lookup
**File**: `assembler/context.py:54-63`
**Severity**: **Low**
**Category**: Performance

`_estimate_tokens()` calls `tiktoken.get_encoding("cl100k_base")` inside the function body on every invocation. `get_encoding()` is memoized internally by tiktoken, so this isn't a performance catastrophe, but it does acquire the tiktoken internal lock on each call. In a hot path (budgeting N facts per turn), this creates unnecessary lock contention.

**Remediation**: Cache the encoding object at module level: `_ENCODING = tiktoken.get_encoding("cl100k_base")`.

---

### F-05: `_budget_facts` greedy selection may leave significant budget unused
**File**: `assembler/context.py:361-369`
**Severity**: **Medium**
**Category**: Correctness / Performance

The greedy budget loop (`for score, fact in scored:`) breaks on the first fact that exceeds the remaining budget. This is a classic greedy knapsack problem — a single large fact can prevent many smaller facts from being considered. The `break` at line 369 stops all further evaluation, even if subsequent facts would fit.

**Impact**: Token budget underutilization. If a 500-token fact at rank 50 doesn't fit, the 20-token facts at ranks 51-200 are never considered.

**Remediation**: Replace `break` with `continue` — skip oversized facts and keep evaluating smaller ones. Or implement a proper knapsack for the final budget fill.

---

### F-06: Context windowing can exceed budget silently
**File**: `assembler/context.py:372-384`
**Severity**: **Medium**
**Category**: Correctness

After `_expand_with_context_window()`, the code re-budgets with a fresh `total_tokens` counter (line 374), but only adopts the windowed list if `len(final) >= len(selected)` (line 383). This length check is wrong — it's comparing *count of facts*, not *token count*. If windowing adds a few short facts but removes none, `final` will have more entries but the total tokens will still be within budget. If windowing adds long facts that push tokens over budget, they're included because the count is higher. The guard should compare token totals, not fact counts.

**Impact**: Token budget overrun when context-windowing adds large facts.

**Remediation**: Compare `total_tokens <= token_budget` instead of `len(final) >= len(selected)`. Or remove the guard and always use the re-budgeted `final` list.

---

### F-07: `__import__("json")` used instead of proper import
**File**: `assembler/query_rewrite.py:155`
**Severity**: **Low**
**Category**: Code Quality / AI Anti-pattern

`content=__import__("json").dumps(payload).encode()` — using `__import__` inline is an AI-generated anti-pattern. The module already imports `re` and `httpx` at the top; `json` should be a proper top-level import.

**Remediation**: Add `import json` at the top of the file and use `json.dumps(payload).encode()`.

---

### F-08: `_llm_semaphore` global — not reset on config change at runtime
**File**: `extractor/client.py:244-267`
**Severity**: **Medium**
**Category**: Correctness

`_llm_semaphore` is initialized once from `settings.extractor_llm_concurrency` and never rebuilt unless `_reset_llm_semaphore()` is called explicitly. If `extractor_llm_concurrency` is changed via the `/admin/config` PATCH endpoint (runtime config override), the semaphore retains the old value. The `_reset_llm_semaphore()` function exists but is never called from the config-update path.

**Impact**: Config changes to LLM concurrency don't take effect until process restart.

**Remediation**: Call `_reset_llm_semaphore()` from the config-update hook (or the admin/config PATCH handler).

---

### F-09: DefaultExtractor reuses full SYSTEM_PROMPT with example — wasteful
**File**: `extractor/extractors/default.py:39-45`
**Severity**: **Low**
**Category**: Performance / Token Economics

`DefaultExtractor` builds a prompt using `build_extraction_prompt()` with empty `user_message` and a synthetic `assistant_response = f"Used tool {tool_name}"`. This pulls in the full `SYSTEM_PROMPT` + `EXAMPLE_PROMPT` (~1200 tokens of system+example context) for what is essentially a simple tool-result extraction. The example prompt trains the model for full-turn extraction but the actual input is a single tool result.

**Impact**: ~$0.0005/turn wasted on unnecessary prompt tokens for default-extracted tools.

**Remediation**: Create a lightweight default-extraction prompt (similar to `BASH_SYSTEM_PROMPT`) that skips the turn-level schema and examples.

---

### F-10: `_parse_extraction_response` has no schema validation
**File**: `extractor/client.py:97-237`
**Severity**: **Medium**
**Category**: Correctness / Robustness

The parser normalizes LLM JSON output but never validates against the declared schema. Malformed or partial responses (missing required keys, wrong types) silently produce empty/default values. For example, if the model returns `{"facts": null}`, `facts` becomes `None` and `data.get("facts", [])` returns `None` (not `[]`), which would crash the downstream `for f in facts:` loop.

**Impact**: Potential `TypeError: 'NoneType' is not iterable` on malformed LLM output.

**Remediation**: Add `facts = facts or []` guards, or validate with a Pydantic model.

---

### F-11: Jaccard dedup threshold not configurable at runtime
**File**: `extractor/dedup.py:33`
**Severity**: **Low**
**Category**: Maintainability

`DEFAULT_SIMILARITY_THRESHOLD = 0.85` is a module constant. While `is_duplicate()` and `deduplicate_facts()` accept a `threshold` parameter, the callers never pass one — they use the default. There's no way to tune this at runtime via `/admin/config`.

**Impact**: Cannot tune dedup sensitivity without code change and redeploy.

**Remediation**: Wire threshold to `settings.dedup_similarity_threshold` with fallback to 0.85.

---

### F-12: `_build_outline` catches bare `ImportError` for javalang — masks install issues
**File**: `shared/text_utils.py:103`
**Severity**: **Low**
**Category**: Maintainability

The `except (SyntaxError, ValueError, ImportError)` block catches `ImportError` if javalang is not installed, but this also masks `ImportError` from a broken javalang installation or a transitive dependency failure. The comment says "Catch parse/lexer errors and ImportError if javalang is not installed" but a corrupted javalang install would silently fail.

**Remediation**: Check for javalang availability once at module load and set a flag, rather than catching ImportError on every call.

---

### F-13: `IssueNode.status` is a bare `str`, not an enum
**File**: `models/graph_nodes.py:87`
**Severity**: **Low**
**Category**: Type Safety

`IssueNode.status` is `str = "open"` with comment `"open" | "resolved"`, while `VerificationNode.status` is also bare `str`. Both should use Enums like `FactType` and `SessionStatus` for type safety and exhaustiveness checking.

**Remediation**: Create `IssueStatus(str, Enum)` and `VerificationStatus(str, Enum)`.

---

### F-14: Bash extractor regex `_ERROR_RE` can produce noisy false positives
**File**: `extractor/extractors/bash.py:73`
**Severity**: **Low**
**Category**: Correctness

`_ERROR_RE = re.compile(r"(?:error|Error|ERROR):?\s+(.{0,120})")` always runs against the full output (line 234). In test output containing `FAILED test_name`, the universal error pattern is also applied, potentially double-extracting the same error as both a `failed_test` and an `error` fact. No dedup runs within a single extractor's output.

**Impact**: Minor — duplicate facts within a single turn's extraction. Cross-turn dedup catches it later.

**Remediation**: Track extracted error lines and skip the universal pattern if already covered by a command-specific pattern.

---

### F-15: `models/enums.py` is a pure re-export shim with no added value
**File**: `models/enums.py:1-5`
**Severity**: **Low**
**Category**: Maintainability

`enums.py` re-exports `FactType`, `FileStatus`, `SessionStatus` from `graph_nodes.py`. The `__init__.py` already exports these directly from `graph_nodes`. This creates two import paths for the same symbols with no clear convention.

**Remediation**: Either remove `enums.py` and update imports, or move the Enum definitions into `enums.py` and have `graph_nodes.py` import from there.

---

### F-16: `compaction.py` checks `embedding_api_key` instead of `extractor_api_key`
**File**: `assembler/compaction.py:49`
**Severity**: **Medium**
**Category**: Correctness / Bug

The compaction module uses `settings.extractor_model` and `settings.extractor_api_key` for the LLM call, but the guard at line 49 checks `settings.embedding_api_key` — a different key. If embeddings are disabled (no embedding key) but extraction is enabled (has extractor key), compaction will be silently skipped even though it doesn't use the embedding key at all.

**Impact**: Compaction (a potentially critical overflow-safety mechanism) is gated behind an unrelated config flag.

**Remediation**: Change guard to `if not settings.extractor_api_key:`.

---

### F-17: Assembler cold-start gate comment contradicts code
**File**: `assembler/context.py:425-448`
**Severity**: **Low**
**Category**: Documentation / Correctness drift

The docstring says "passthrough when BOTH conditions hold" but the code implements `and` (both must be true for passthrough). The inline comments at lines 429-434 are correct — "Either condition alone lets assembly fire." The docstring is consistent with the code but the word "BOTH" in all-caps could be misleading if read in isolation. No actual bug.

---

## Structured Summary

| ID | File:Line | Severity | Category | Finding |
|----|-----------|----------|----------|---------|
| F-01 | extractor/client.py:313 | **Critical** | Concurrency | TOCTOU race in dedup+store — concurrent turns bypass dedup |
| F-02 | extractor/dedup.py:96 | **High** | Observability | Dedup ratio not emitted as structured metric, no alerting |
| F-03 | assembler/context.py:38 | Medium | Thread Safety | Module-level `_embedding_cache` dict has no concurrency protection |
| F-04 | assembler/context.py:54 | Low | Performance | tiktoken encoding re-looked-up on every `_estimate_tokens()` call |
| F-05 | assembler/context.py:361 | Medium | Correctness | Greedy budget loop `break` leaves remaining budget unused |
| F-06 | assembler/context.py:383 | Medium | Correctness | Context-windowing guard compares fact count, not token count |
| F-07 | assembler/query_rewrite.py:155 | Low | AI Anti-pattern | `__import__("json")` instead of top-level import |
| F-08 | extractor/client.py:244 | Medium | Correctness | `_llm_semaphore` not rebuilt on runtime config change |
| F-09 | extractor/extractors/default.py:39 | Low | Token Waste | DefaultExtractor uses full SYSTEM_PROMPT+EXAMPLE for single-tool extraction |
| F-10 | extractor/client.py:127 | Medium | Robustness | `_parse_extraction_response` — no None-guard on `facts` from `data.get()` |
| F-11 | extractor/dedup.py:33 | Low | Maintainability | Dedup threshold not configurable at runtime |
| F-12 | shared/text_utils.py:103 | Low | Maintainability | Bare `ImportError` catch for javalang masks install issues |
| F-13 | models/graph_nodes.py:87 | Low | Type Safety | `IssueNode.status` and `VerificationNode.status` are bare `str`, not enums |
| F-14 | extractor/extractors/bash.py:234 | Low | Correctness | Universal error regex double-extracts errors already caught by command-specific patterns |
| F-15 | models/enums.py:1 | Low | Maintainability | Pure re-export shim with no added value, creates dual import paths |
| F-16 | assembler/compaction.py:49 | Medium | Bug | Compaction guard checks `embedding_api_key` but uses `extractor_api_key` |
| F-17 | assembler/context.py:425 | Low | Doc | Cold-start comment could be clearer |

### Severity Distribution

| Severity | Count |
|----------|-------|
| Critical | 1 |
| High | 1 |
| Medium | 6 |
| Low | 9 |

### Known Concerns Status

| Concern | Status | Finding |
|---------|--------|---------|
| Extraction race condition | **Confirmed** | F-01: TOCTOU between concurrent turn dedup+store |
| Dedup ratio monitoring | **Confirmed** | F-02: No structured metric, no alert threshold |

### AI Anti-patterns Detected

1. **`__import__` hack** — `query_rewrite.py:155` (F-07)
2. **No schema validation on LLM output** — `client.py:97-237` (F-10) — trusting LLM JSON without Pydantic validation is a common AI-code pattern
3. **Wrong config key guard** — `compaction.py:49` (F-16) — likely copy-paste error from embedding code path

### Positive Observations

- Clean separation of concerns: assembler, extractor, models, shared layers are well-bounded
- Graceful degradation throughout: Neo4j failures, embedding failures, LLM failures all fall back to passthrough
- Good use of structlog with structured fields for observability
- Intent-driven fact scoring is a sophisticated and well-documented approach
- Regex-first extraction in BashExtractor with LLM fallback is cost-efficient
- Smart coherence tail (tail.py) correctly handles orphaned tool messages — defensive design
- Token budget architecture (anchors → scored candidates → context windowing) is sound
