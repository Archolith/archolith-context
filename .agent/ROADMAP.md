# archolith-context — Context Quality Roadmap

Derived from the 2026-05-26 end-to-end curator pipeline review.  Items are
organized by horizon, not by strict priority within a horizon.  Value and effort
are relative to the current system baseline.

---

## Done

| Item | Commit | Notes |
|------|--------|-------|
| Pre-inject checkpoint into curator prompt | `27ec4a7` | Saves one full LLM iteration (~1-2s) per curator run |
| Fix prompt cache instability — remove per-turn counter from stable section | `27ec4a7` | Moved `Current turn: N` out of `=== SESSION OVERVIEW ===` into facts footer |
| Write tool content → file cache directly from tool_call args | `27ec4a7` | `_extract_file_writes()` in chat.py; skips Edit (patch application needed) |
| RTK Layer 1 filter on extraction budget | `fb76ddc` | `filter_single_tool_result` in `_collect_recent_tool_results` — extractor LLM sees clean signal |
| RTK Layer 2 shrink on coherence tail | `fb76ddc` | `shrink_tail_tool_results` in `rewrite_messages()` — tail tool msgs capped at 2000 tokens |
| RTK Layer 2 shrink on outbound tool_call args | `fb76ddc` | `shrink_tool_call_args` in `filter_request_body` — Write/Edit file content collapsed |
| Unify `_wrap_response_as_sse` / `_non_streaming_to_sse` | `2b021da` | All SSE formatting in `streaming.py`; `yield_as_sse` helper removes last `\n\n` site from `chat.py` |
| LadybugDB WAL corruption — base fix | `78e2d81` | `throw_on_wal_replay_failure=False`; partial WAL replay on force-kill |
| LadybugDB WAL resilience — four improvements | `5fe067b` | WAL detection logging, auto-rotate on failed probe, 16 MB checkpoint threshold, atexit registration |
| File structure index on cache ingest | `94e182d` | `FileOutline` table, `_build_outline` (AST + regex), `get_file_outline` as 12th curator tool; rule 3 updated |
| Semantic search over facts | `TBD` | `search_facts_semantic` (13th tool) — cosine similarity on stored embeddings; falls back to substring; 15 tests |

---

## Next

Scoped, medium-effort items.  Each is self-contained and can land in a single session.

### RTK inter-turn compression of coherence tail inner messages

**Value:** High | **Effort:** Medium

The coherence tail currently keeps every message intact except tool-role token capping
(done).  The middle portion of the tail (messages that are not in the final N turns)
can be further compressed using the RTK `filter_output` cross-turn `DedupeTracker` —
repeated patterns (repeated grep results, repeated file content fragments) are collapsed
across turns.

**Shape:**
- In `rewrite_messages()`, replace the manual `_is_compressible_tool` char-truncation
  loop with a call to `filter_output(content, tool=tool_name)` per tool-role middle
  message — this routes each result through the correct category filter and the
  singleton `DedupeTracker` simultaneously
- `DedupeTracker` is already public in archolith-rtk (`__all__`); no changes to
  archolith-rtk required — all work is in archolith-context `rewrite.py` and `rtk.py`
- Add `filter_middle_tool_results(messages) -> list[dict]` to `rtk.py` as the
  fail-open adapter (analogous to `filter_tool_messages` but scoped to middle-section
  non-tail messages)
- The singleton `DedupeTracker` is process-level, so cross-turn dedup between the
  middle pass and the later `filter_request_body` pass is automatic — no explicit
  threading required
- **Blocked on archolith-rtk**: the `archolith-rtk-cross-turn-dedupe-plan` workspace plan improves
  the `DedupeTracker` within RTK first (fingerprinting, compact markers, recovery IDs). The
  archolith-context wiring (swap compressible-tool loop for `filter_output` call) is a follow-on
  once the RTK-side plan lands — it will inherit the improved dedup automatically.

---

## Plan

Items that need a design pass or span multiple files in a non-trivial way.

### Per-tool extraction (structured output per Bash / Read / Grep)

**Value:** High | **Effort:** High

The extractor currently receives a flat concatenation of all tool results and asks the
model to extract facts, decisions, and files in one shot.  The extraction quality is
limited because different tools produce fundamentally different output types:

| Tool | Ideal extraction |
|------|-----------------|
| Read | File path → content → update FileContent cache; extract symbols defined |
| Bash | Command → exit code → structured output (detect test pass/fail, build errors) |
| Grep | Pattern → matches → "symbol X appears in file Y at line Z" facts |
| Write / Edit | Path → new content → update FileContent cache |

**Shape:**
- Add per-tool extraction prompts in `extractor/prompts.py`
- Route in `_collect_recent_tool_results()`: classify by tool name, call appropriate
  prompt, merge results before storing
- Structured output (JSON mode) per tool type rather than freeform extraction
- LadybugDB schema: add `fact_source_tool` field for filtering and attribution

---

### Curator output caching between turns

**Value:** Medium | **Effort:** Medium

The curator runs on every turn even when the session state hasn't changed materially
(same files in cache, same facts, no new decisions).  A lightweight "context diff"
check could detect turn-over-turn staleness and serve the cached curator output when
the new question doesn't require fresh retrieval.

**Shape:**
- Add `curator_cache: dict[str, CuratorResult]` in session state (in-memory, TTL 1 turn)
- Hash key: `sha256(session_goal + user_message[:100] + sorted(touched_files))`
- If cache hit and hash matches → return cached result, skip LLM call
- Cache invalidated on: new fact stored, new file cached, new decision recorded
- Metric: `curator_cache_hits`

---

### Adaptive tail sizing by intent

**Value:** Medium | **Effort:** Low

`COHERENCE_TAIL_SIZE` is a static config knob (default 10).  The tail should expand
when the question signals multi-turn continuity ("continue what we were doing", "fix
the failing test", "now do the same for the other file") and contract when the question
is a fresh pivot ("let's start on a new feature", "ignore everything above").

**Shape:**
- Add `classify_turn_intent(user_message) -> Literal["continue", "pivot", "neutral"]`
  in `assembler/tail.py` — rule-based regex, no LLM call
- Adjust `smart_tail()` base size: `continue` → base+4, `pivot` → max(base-3, 3)
- Log `tail_intent` and `tail_size_actual` in trace for benchmarking

---

### Goal-drift detection → aggressive re-weighting

**Value:** Medium | **Effort:** Medium

When the session goal was set at turn 1 but the agent has pivoted to a completely
different task by turn 20, the assembler continues to weight facts relative to the
original goal.  Detecting goal drift allows the system to either update the goal
automatically or at minimum down-weight facts from the pre-drift period.

**Shape:**
- Add `detect_goal_drift(session_goal, recent_messages, facts) -> float` — cosine
  similarity between goal embedding and the last 3 user messages; score < 0.4 = drift
- On drift detected: `assembly_mode = "goal_drift"` in trace; re-run fact scoring
  relative to the last user message instead of the stored goal
- Optional: surface drift signal to the curator via a `goal_drifted` flag in the prompt

---

## Later

High-effort items with foundational dependencies or unclear implementation shape.
Worth revisiting once Next and Plan tiers are done.

### Graph-topology-aware retrieval

**Value:** High | **Effort:** High

Currently the assembler retrieves facts by relevance score alone.  The session graph
contains temporal edges (FOLLOWS, DEPENDS_ON, INVALIDATES) that encode causal chains.
A fact that is transitively connected to the current question's files/entities via
multiple hops is likely more relevant than a high-scoring but isolated fact.

**Shape:**
- Add graph traversal queries to `graph/backend.py`: `get_related_facts(entity, depth=2)`
- Score facts as: `base_score * (1 + hop_bonus * (1/hops))` where hop_bonus is tunable
- Requires Neo4j (graph traversal is not practical in LadybugDB without an index)
- May be superseded by embedding-based retrieval if semantic search proves sufficient

---

### Embedding-based fact dedup / consolidation

**Value:** Medium | **Effort:** High

The session graph accumulates facts with minor semantic variations across turns ("the
auth middleware is at line 42" vs "auth middleware lives in middleware/auth.py at L42").
Embedding-based dedup would merge near-duplicate facts before they can cause the
assembler to include redundant context.

**Shape:**
- On every `upsert_fact()`, embed the new fact and compare against the 20 most recent
  active facts for the same entity type
- If cosine similarity > 0.92 → mark older fact as `superseded`, keep newer
- Requires embedding on every fact write — adds ~50ms per extraction turn at scale
- Consider batching: embed and dedup in the background async extraction path only

---

## Out of Scope / Deferred

| Item | Reason |
|------|--------|
| Synthetic tools (`SYNTHETIC_TOOLS_ENABLED`) | Direction change — capturing native tool usage instead; synthetic tooling not pursued |
| Nous XML fallback curator loop | `_run_curator_nous()` kept as dead code for now; remove if no model requires it by next review |
| Neo4j file cache stubs | File cache is LadybugDB-only by design in MVP; Neo4j stubs return None/[] and that is intentional |
