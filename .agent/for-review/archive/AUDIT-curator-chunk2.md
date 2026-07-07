# Curator Subsystem Audit — Chunk 2

**Auditor:** z-ai/glm-5.1 (OpenCode)
**Date:** 2026-06-07
**Scope:** `archolith_proxy/curator/` (11 files, ~2,600 LOC)
**Commit:** UNCOMMITTED (audit only, no code changes)

---

## Summary

The curator subsystem implements an LLM-driven context manager with a tool-calling loop (up to 6 iterations, 6s budget), 15 curator tools (14 original + `score_file_relevance`), two-pass mode (background prepper + inline assembler), and a briefing cache. Overall architecture is sound: separation of concerns is clean across `loop.py` (LLM loop), `tools.py` (tool implementations), `state.py` (caching), `pipeline.py` (dispatch), `briefing.py` (data + formatting), `prepper.py` / `assembler.py` (two-curator mode), `prompts.py` (prompt construction), `schemas.py` (tool definitions), and `result.py` (data types).

Key findings: one critical path-traversal hole in `prefetch_file`, one high-severity token-cost leak from briefing injection, and several medium-severity robustness gaps in the background task lifecycle and briefing parsing. All four prior-audit concerns are partially or fully confirmed.

---

## Findings

### F-01 — Path Traversal in `prefetch_file` (prior to allowlist enforcement)

**File:** `tools.py:462–488`
**Severity:** Critical
**Prior Audit:** Not previously flagged

When `prefetch_allowed_roots` is empty (default), `prefetch_file` accepts any absolute path — including `/etc/shadow`, `C:\Windows\System32\config\SAM`, etc. Even when the allowlist is set, the relative-path resolution logic (lines 462–488) walks up to 8 parent directories from cached file roots, creating a ladder that can escape the allowlist boundary. The second allowlist check at line 491–497 validates the *resolved* path, but the parent-walking loop at line 476–483 resolves `candidate / path` against *each parent* — a relative path like `../../etc/passwd` would resolve against a parent inside the allowlist and then walk up past it.

**Impact:** LLM-driven SSRF/path-traversal — the curator bot can be prompted to read any file on the host filesystem. The content is then cached and returned into the LLM context, potentially exfiltrating secrets.

**Recommendation:** Enforce `prefetch_allowed_roots` as non-empty in production configs. Add a symlink-following check: `file_path.resolve()` must still satisfy the allowlist after resolution. Reject any path containing `..` components outright.

---

### F-02 — Token Cost Leak: Full Briefing Dumped into User Prompt

**File:** `assembler.py:152`, `pipeline.py:242`
**Severity:** High
**Prior Audit:** Flagged as concern (4) "token cost leak from dumping entire briefing into user prompt"
**Status:** **Confirmed.** Both `_run_with_briefing` and `run_assembler` prepend the entire formatted briefing (up to 30K chars / ~10K tokens) directly into the user prompt via string concatenation:

```python
user_prompt = briefing_text + "\n\n---\n\n" + base_prompt
```

This means every inline pass re-sends the full briefing as input tokens. For a 30K-char briefing, that's ~10K input tokens on every turn — even when the briefing is stale or the current question is trivial.

Additionally, `format_briefing_for_prompt` (briefing.py:174–238) includes *full raw file contents* in the `RELEVANT CODE` section (line 203–204), not just outlines. A briefing with 5 files × 80 lines each adds ~15K chars of raw code to the prompt.

**Impact:** ~10K wasted input tokens per turn on top of the assembler's own output. At scale, this doubles the curator's per-turn token cost.

**Recommendation:** (1) Replace full file content with outlines + section references in the briefing format. The assembler can call `get_file_lines` if it needs the actual code. (2) Add a token budget to `format_briefing_for_prompt` that caps the briefing at ~2K tokens and uses outline-only representation. (3) Consider storing the briefing in the snapshot cache and passing a reference instead of the full text.

---

### F-03 — Background Task Runaway: No Cancellation on Prepper Path

**File:** `extraction.py:377–386`, `state.py:101–115`
**Severity:** High
**Prior Audit:** Flagged as concern (1) "background task runaway — no cancellation logic"
**Status:** **Partially Mitigated.** `swap_background_task` (state.py:101–115) now cancels the previous task before registering a new one. This is correct for the *superseded-by-next-turn* case. However:

1. **No timeout enforcement for the two_curator prepper path.** The prepper uses `asyncio.wait_for` with `prepper_latency_budget_ms` (30s), but the background task itself (created at extraction.py:377) is not given a separate deadline. If the prepper's LLM call hangs beyond 30s, `wait_for` raises `TimeoutError` inside the task, which is caught and returns `None`. But if the OpenAI client's own HTTP timeout exceeds `wait_for`'s deadline, the underlying connection remains open until the OS-level socket timeout fires (typically 60–120s). During that window, the task is technically "done" (wait_for raised), but the HTTP connection is still consuming a file descriptor.

2. **Orphaned completions under rate limits.** When a prepper is cancelled (via `swap_background_task`), the CancelledError propagates into `_run_curator_native`, but any in-flight `_llm_call_with_retry` call has already been sent to the upstream API. The cancellation just drops the result — the upstream API still processes and bills the request. Under rate limits, two rapid turns can trigger two prepper calls, with the first cancelled mid-flight, burning both rate limit quota and cost.

**Recommendation:** (1) Set an explicit `httpx.Timeout(timeout=30.0)` on the `AsyncOpenAI` client in `prepper.py:139` so the HTTP connection is torn down at the budget deadline. (2) Track in-flight request IDs and consider aborting via the OpenAI cancel endpoint (if available). (3) Add a cooldown in `swap_background_task`: if a task was cancelled within N seconds of starting, don't launch a new one for the same session for M seconds.

---

### F-04 — Extraction Race Condition: Debounce Insufficient for Slow Extraction

**File:** `pipeline.py:64–67`, `config.py:196`
**Severity:** Medium
**Prior Audit:** Flagged as concern (2) "extraction race condition — debounce timer insufficient for slow extraction"
**Status:** **Confirmed.** The background pass debounce is 2000ms (config.py:196). The background pass sleeps for this duration (`pipeline.py:65–67`) to wait for extraction to finish. However:

- Extraction latency varies by upstream model. DeepSeek extraction can take 3–8s per turn. A 2s debounce means the background pass starts while extraction is still writing facts to the graph. The prepper then reads stale/partial fact data.
- The two_curator prepper path has `prepper_debounce_ms = 2000` (config.py:210), but this config field is **never read by `run_prepper`** — the prepper is invoked directly from `run_background_pass`, which uses the *global* `background_pass_debounce_ms`. The prepper-specific debounce is dead config.

**Impact:** Background pass reads incomplete extraction results, producing a briefing based on partial data. The next inline pass then inherits a stale briefing.

**Recommendation:** (1) Either make the debounce configurable per-mode or read `prepper_debounce_ms` in the two_curator path. (2) Consider a more robust approach: have the background pass wait for an extraction-completion signal (e.g., a per-session asyncio.Event) rather than a fixed sleep.

---

### F-05 — Assembler Fallback Deficit: 2 Tools vs Full 14

**File:** `schemas.py:327–331`, `assembler.py:92–211`
**Severity:** Medium
**Prior Audit:** Flagged as concern (3) "assembler fallback deficit — assembler lacks full 13 tools for fallback, must use original curator loop instead"
**Status:** **Confirmed.** The assembler tool set is exactly 2 tools: `select_relevant_turns` and `get_file_lines` (schemas.py:327–331). When the assembler returns `None` (failure), `curate_context` (pipeline.py:298–423) falls through to the full curator loop with `ALL_CURATOR_TOOLS` (14 tools) — this is the intended fallback path.

The issue is that the assembler failure rate is non-trivial: any timeout, LLM error, or empty response causes a `None` return. The fallback then runs a *full* 4–6 iteration curator loop, adding 3–6s latency on top of the already-failed assembler attempt (up to 3s). Total worst-case: 9s+ inline latency.

**Impact:** Tail latency spikes when assembler fails. The fallback is correct but slow.

**Recommendation:** (1) Add an intermediate fallback: if the assembler fails but a briefing exists, try a single-iteration curator call with the briefing as context but with a larger tool set (e.g., all tools except `score_file_relevance`). (2) Reduce the assembler timeout from 3s to 2s to leave more budget for the fallback. (3) Track assembler failure rate in metrics and alert when it exceeds 15%.

---

### F-06 — Module-Level Mutable Globals (Unbounded Growth)

**File:** `state.py:38–42,98`, `pipeline.py:33`
**Severity:** Medium
**Prior Audit:** Not previously flagged

Three module-level dicts grow without bound:
- `_cache` (state.py:38) — per-session snapshots
- `_briefing_cache` (state.py:42) — per-session briefings
- `_bg_tasks` (state.py:98) — per-session background tasks
- `_last_attempt` (pipeline.py:33) — per-session failure diagnostics

`prune_session_state` (state.py:125–135) and `prune_last_attempts` (pipeline.py:41–49) exist but require an explicit call with the set of active session IDs. If the caller doesn't invoke them (or invokes them too infrequently), these dicts grow unboundedly.

**Impact:** Memory leak in long-running processes with many sessions.

**Recommendation:** (1) Add a max-size cap (e.g., LRU eviction at 1000 sessions). (2) Schedule periodic pruning via the background cleanup loop in `main.py`. (3) Add telemetry for dict sizes in the `/health` endpoint.

---

### F-07 — `AsyncOpenAI` Client Created Per-Call (No Connection Reuse)

**File:** `pipeline.py:324`, `prepper.py:139`
**Severity:** Medium
**Prior Audit:** Not previously flagged

Each call to `curate_context` and `run_prepper` creates a new `AsyncOpenAI` client instance. The OpenAI Python SDK creates a new `httpx.AsyncClient` per `AsyncOpenAI` instance, which means:
- No HTTP connection pooling across curator calls
- TCP handshake overhead on every turn
- Potential file descriptor exhaustion under high concurrency

**Recommendation:** Create a long-lived `AsyncOpenAI` client at module level or in the settings object, similar to the `_semantic_client` pattern in `tools.py:22–35`.

---

### F-08 — `extract_section` Regex Doesn't Handle Malformed Context Blocks

**File:** `briefing.py:72–78`
**Severity:** Low
**Prior Audit:** Not previously flagged

The regex `rf"=== {section_name} ===\s*\n(.*?)(?=\n=== .+? ===|$)"` assumes section headers are exactly `=== NAME ===`. If the LLM emits `===NAME===` (no spaces) or `=== NAME ===  ` (trailing whitespace), the regex fails silently and returns `""`. This is a correctness issue in `build_briefing_from_result` — entire sections of the context block can be lost.

**Impact:** Briefing appears to have no checkpoint/issues/facts even when the curator produced them. The assembler then re-fetches the same data.

**Recommendation:** Make the regex more tolerant: `rf"===\s*{section_name}\s*===(?:\s*)\n(.*?)(?=\n===\s*.+?\s*===(?:\s*)|$)"`. Add a fallback: if regex extraction yields nothing, do a simple string search for the section header.

---

### F-09 — `_cosine` Similarity in `search_facts_semantic` is Unvectorized

**File:** `tools.py:160–168`
**Severity:** Low
**Prior Audit:** Not previously flagged

The `_cosine` function computes dot product and magnitudes with Python `sum()` and list comprehensions. For 200 facts × 1536-dim embeddings, this is ~300K multiply-adds in pure Python — likely 10–100ms per call. This is acceptable for low-volume usage but will become a bottleneck if semantic search is called frequently.

**Recommendation:** If `numpy` is available, use `numpy.dot` / `numpy.linalg.norm`. Consider adding `numpy` as an optional dependency.

---

### F-10 — `select_relevant_turns` Handler is a No-Op Side Channel

**File:** `tools.py:315–327`
**Severity:** Low
**Prior Audit:** Not previously flagged

The `select_relevant_turns` handler just returns a confirmation string — it doesn't actually modify any state. The actual retention is captured in `loop.py:374–377` by reading `args.turn_numbers` and storing it in the loop-local `retained_turn_numbers`. This means:

1. The tool "succeeds" from the LLM's perspective (returns a confirmation), but the real work happens as a side effect of the loop's argument parsing.
2. If the LLM calls `select_relevant_turns` multiple times, only the last call's value is used (previous values are silently overwritten). No warning is emitted.

**Impact:** Correct but confusing. Multiple calls silently overwrite.

**Recommendation:** (1) If `select_relevant_turns` is called more than once, emit a PROXY-NOTE warning (similar to the repeated-file-read warning). (2) Consider moving the retention capture into the tool handler itself for clearer ownership.

---

### F-11 — `prefetch_file` Relative Path Resolution Walks 8 Levels Up

**File:** `tools.py:474–485`
**Severity:** Low
**Prior Audit:** Not previously flagged

The relative path resolution walks up to 8 parent directories from cached file roots. This is a broad search that could match unintended files. Combined with F-01, this expands the attack surface.

**Recommendation:** Reduce to 2 levels or require the LLM to use absolute paths only (the schema description already says "Prefer absolute paths").

---

### F-12 — Duplicate `__all__` in `pipeline.py`

**File:** `pipeline.py:295,426`
**Severity:** Low
**Prior Audit:** Not previously flagged

`__all__` is defined twice at lines 295 and 426. The second definition (after `curate_context`) overrides the first. Both are identical, so this is harmless but confusing.

**Recommendation:** Remove the first `__all__` at line 295.

---

### F-13 — `score_file_relevance` Recency Score Inverted

**File:** `tools.py:375–379`
**Severity:** Low
**Prior Audit:** Not previously flagged

The recency score `min(3.0, last_turn * 0.5)` increases with higher turn numbers, meaning *later* files score higher. This is intentional (recently-active files are more relevant), but the variable name `last_turn` is ambiguous — it could mean "last turn the file was updated" or "the current turn number". If `last_turn` represents the turn when the file was last updated, the scoring is correct. But if a file was last updated at turn 2 and the current turn is 10, it gets a low recency score (1.0), which is correct.

**Impact:** Correct behavior, but the variable naming and the lack of a comment make this fragile for future maintainers.

---

### F-14 — Prepper Skips Debounce Entirely

**File:** `prepper.py:90–193`, `pipeline.py:166–213`
**Severity:** Medium
**Prior Audit:** Related to concern (2)

The two_curator prepper path (`run_prepper`) is called directly from `run_background_pass` (pipeline.py:183–186). The debounce sleep only happens in the default (`_run_background_pass_inner`, pipeline.py:65–67). When `_background_pass_fn` is set (i.e., the prepper), `run_background_pass` calls the prepper *immediately* with no debounce wait. This means the prepper can start before extraction has written any data.

**Impact:** Prepper reads stale graph data, produces a briefing that doesn't reflect the latest turn's extraction. This partially invalidates the prepper's value proposition.

**Recommendation:** Add the debounce sleep before calling `_background_pass_fn` in `run_background_pass`, or implement the event-based wait described in F-04.

---

## Import DAG Analysis

```
__init__.py
  ├─> config (get_settings)
  ├─> briefing (SessionBriefing)
  ├─> pipeline (curate_context, get_last_attempt, run_background_pass)
  └─> models.dtos (AssembledContext)

pipeline.py
  ├─> config (get_settings)
  ├─> briefing (SessionBriefing, format_briefing_for_prompt, build_briefing_from_result)
  ├─> prompts (CURATOR_SYSTEM_PROMPT, build_curator_user_prompt)
  ├─> state (CuratorSnapshot, cache_briefing, cache_snapshot, get_briefing, get_snapshot, is_briefing_fresh)
  ├─> models.dtos (AssembledContext)
  ├─> loop (_run_curator_native) [deferred import]
  ├─> graph.backend (get_backend, is_graph_ready) [deferred import]
  ├─> metrics (record_metric) [deferred import]
  └─> trace.store (get_trace_store) [deferred import]

loop.py
  ├─> result (CuratorFailure, CuratorResult, CuratorToolCall)
  ├─> schemas (ALL_CURATOR_TOOLS)
  ├─> tools (TOOL_HANDLERS)
  └─> config (get_settings) [deferred import inside _save_failure_diagnostic]

tools.py
  ├─> graph.backend (get_backend)
  ├─> shared.text_utils (_build_outline)
  ├─> config (get_settings) [deferred import in search_facts_semantic, prefetch_file]
  └─> extractor.embeddings (compute_embeddings_batch) [deferred import]

prepper.py
  ├─> config (get_settings)
  ├─> briefing (SessionBriefing, build_briefing_from_result)
  ├─> loop (_run_curator_native)
  ├─> schemas (PREPPER_TOOLS)
  ├─> state (get_snapshot)
  ├─> prompts (build_curator_user_prompt)
  └─> graph.backend (get_backend, is_graph_ready) [deferred import]

assembler.py
  ├─> config (get_settings)
  ├─> briefing (SessionBriefing, format_briefing_for_prompt)
  ├─> loop (_run_curator_native)
  ├─> prompts (build_curator_user_prompt)
  ├─> schemas (ASSEMBLER_TOOLS)
  ├─> state (CuratorSnapshot, cache_snapshot, get_snapshot)
  ├─> models.dtos (AssembledContext)
  └─> graph.backend (get_backend, is_graph_ready) [deferred import]

prompts.py
  └─> assembler.tail (smart_tail) [deferred import]

schemas.py — leaf node, no imports from curator submodules

state.py
  └─> briefing (SessionBriefing)

result.py
  └─> pydantic (BaseModel, Field)

briefing.py — leaf node, no imports from curator submodules
```

**Cycles:** None detected. The DAG is clean — all cross-submodule imports are unidirectional. Deferred imports (`loop.py → config`, `pipeline.py → loop`, `prompts.py → assembler.tail`) avoid would-be cycles. The `__init__.py → pipeline` direction is a top-level re-export, not a cycle.

**External dependency chains (not cycles, but deep):**
- `tools.py → graph.backend → Neo4j/cypher` (deepest I/O path)
- `tools.py → extractor.embeddings → httpx → embedding API`
- `loop.py → openai → upstream LLM API`

---

## Prior Audit Concerns — Status Table

| # | Concern | Status | Finding | Detail |
|---|---------|--------|---------|--------|
| 1 | Background task runaway — no cancellation | **Partially Mitigated** | F-03 | `swap_background_task` cancels superseded tasks, but orphaned in-flight HTTP requests and rate-limit waste remain |
| 2 | Extraction race condition — debounce insufficient | **Confirmed** | F-04, F-14 | 2s debounce is too short; prepper skips debounce entirely; `prepper_debounce_ms` is dead config |
| 3 | Assembler fallback deficit — lacks full tools | **Confirmed** | F-05 | 2-tool assembler falls through to full 14-tool curator loop on failure, adding 3–6s tail latency |
| 4 | Token cost leak from briefing dump | **Confirmed** | F-02 | Up to 30K chars (~10K tokens) of briefing injected into every inline prompt, including full file contents |

---

## Metrics Table

| File | Lines | Functions | Async Functions | Imports (internal) | Imports (external) |
|------|-------|-----------|-----------------|--------------------|--------------------|
| `__init__.py` | 44 | 3 | 0 | 3 | 1 (structlog) |
| `tools.py` | 650 | 15 | 8 | 3 | 2 (structlog, httpx) |
| `state.py` | 143 | 8 | 0 | 1 | 2 (asyncio, dataclasses) |
| `schemas.py` | 333 | 1 | 0 | 0 | 0 |
| `result.py` | 80 | 1 | 0 | 1 | 2 (time, pydantic) |
| `prompts.py` | 259 | 3 | 0 | 1 (deferred) | 0 |
| `prepper.py` | 196 | 1 | 1 | 6 | 3 (asyncio, time, openai) |
| `pipeline.py` | 426 | 5 | 4 | 5+4(deferred) | 3 (asyncio, time, openai) |
| `loop.py` | 413 | 4 | 2 | 3 | 5 (asyncio, json, random, pathlib, openai) |
| `briefing.py` | 247 | 3 | 0 | 0 | 2 (re, dataclasses) |
| `assembler.py` | 214 | 1 | 1 | 6+1(deferred) | 2 (asyncio, openai) |
| **Total** | **2,805** | **45** | **16** | — | — |

---

## Recommendations (Priority-Ordered)

1. **[Critical]** Fix path traversal in `prefetch_file` — enforce non-empty `prefetch_allowed_roots` in production, reject `..` components, add symlink-aware resolution check. (F-01)
2. **[High]** Reduce briefing injection size — switch to outline-only representation in `format_briefing_for_prompt`, cap at ~2K tokens instead of 30K chars. (F-02)
3. **[High]** Add HTTP timeout to prepper's `AsyncOpenAI` client to match the `wait_for` budget. (F-03)
4. **[Medium]** Fix prepper debounce — apply `prepper_debounce_ms` (or the global debounce) before calling `_background_pass_fn` in `run_background_pass`. (F-04, F-14)
5. **[Medium]** Add intermediate fallback between assembler failure and full curator loop. (F-05)
6. **[Medium]** Add LRU eviction caps to module-level caches. (F-06)
7. **[Medium]** Reuse `AsyncOpenAI` client instances across calls. (F-07)
8. **[Low]** Harden `extract_section` regex against LLM formatting variance. (F-08)
9. **[Low]** Add PROXY-NOTE for duplicate `select_relevant_turns` calls. (F-10)
10. **[Low]** Remove duplicate `__all__` in `pipeline.py`. (F-12)
