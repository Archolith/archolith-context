# Changelog

## [unreleased] — 2026-06-16 — R3a: dependency extractor path resolution (coverage + precision)

Upgrades `curator/dependency_graph.py` reference resolution so topological in-degree reflects more of
a real corpus (rung-3 follow-up). Public API unchanged; the deterministic assembler is untouched.

- **Relative imports** (`./x`, `../x`) resolve against the importing file's directory, so colliding
  basenames (several `types.ts`) edge to the importer's OWN target, not a random same-named file.
- **Alias / absolute-ish** specs (`@/x/y`, `~/x`, `a/b`) match as a path SUFFIX (no corpus-specific
  alias root hardcoded).
- **Barrel imports** resolve `<dir>/index.*` (`from '@/ui'` -> `ui/index.ts`).
- Bare-word / dotted (HTML `href="mobile.css"`, Python `import a.b.c`) keep the basename fallback.
- **Measured on the real forked/yawn.frontend corpus:** edges 471 -> 562 (+91); files with an outgoing
  edge 58% -> 61%; surfaced barrel foundations `domain/models/index.ts` (in-degree 47) and
  `ui/index.ts` (36) that were previously invisible.
- **tests**: +5 in `test_dependency_graph.py` (alias suffix, barrel index, relative dir-index,
  collision disambiguation, extensionless relative). Full suite 1121 passed.

## [unreleased] — 2026-06-16 — Layer 2: topological fill for the deterministic assembler

Ports the winning strategy from `scripts/assembly_strategy_sweep.py` into a real assembler
fill policy (deterministic-layers direction, rung 2). The sweep showed a pure topological
sort — order files by dependency in-degree so structural FOUNDATIONS survive budget
truncation — beats the Phase-4 scorer for anchor survival, with no LLM and no importance
signal. The sweep used a hand-written dependency map; this derives the same edges
mechanically from file contents so it works on any corpus. The LLM curator/prepper is
UNCHANGED. Off by default; precedence is topological > scored > FIFO.

- **New `curator/dependency_graph.py`**: cheap, corpus-agnostic edge extractor (ES
  `import`/`from`, CommonJS `require`, HTML `href`/`src`, CSS `@import`/`url()`, Python
  `import`), matched by basename against the briefing's own file set; self-edges and
  out-of-set refs excluded. `extract_dependencies` / `compute_indegree` / `order_by_topology`.
- **deterministic_assembler.py**: `build_deterministic_context(..., topological=False)` adds
  the topological fill branch (takes precedence over `scored`); `run_deterministic_assembler`
  reads `assembler_topological_fill`. FIFO and scored paths byte-identical when off.
- **config.py**: `assembler_topological_fill` (off by default).
- **Parity check**: on the real seeded corpus the extractor ranks `api.js` and `mobile.css`
  as the top foundations (in-degree 8 each) — matching the sweep's hardcoded assumption.
- **tests**: new `test_dependency_graph.py` (11) + 3 topological assembler tests. Full suite
  1116 passed. HONEST CAVEAT: topological quality rests entirely on the mechanical extraction
  (corpus-specific); unknown reference styles yield no edge (fall back to FIFO), never a wrong edge.

## [unreleased] — 2026-06-15 — Phase 0.5: single-leader worker leasing via archolith-maintenance

Extracts the proven cross-process lease from cth.memory into a new shared package
`archolith-maintenance` (cth.memory left untouched as the reference) and wires the curator
worker to it. When `curator_worker_lease_enabled`, only the process holding the
`curator-worker` lease runs workers, so two proxy processes sharing a session graph don't
both curate it (de-risks the multi-writer critique). Off by default; without the lease the
registry is always leader (behavior unchanged).

- **New dep**: `archolith-maintenance` (editable: `pip install -e ../archolith-maintenance`),
  provides `SchedulerLeaseStore` (SQLite single-leader lease).
- **curator/worker.py**: `WorkerRegistry` accepts a `lease_store`; `_is_leader()` (cheap,
  re-checks at most once per third of the lease duration) gates `enqueue`; renews on the idle
  tick; releases on `shutdown_all`. `get_worker_registry()` builds the lease store from settings.
- **config.py**: `curator_worker_lease_enabled` (off), `curator_worker_lease_db_path`
  (default alongside the ladybug DB), `curator_worker_lease_duration_s` (90).
- **metrics**: `curator_worker_lease_held` / `curator_worker_lease_blocked`.
- **tests**: 3 worker-lease integration tests (no-lease=leader, follower blocked, release->takeover).
  Full suite 1065 passed.

## [unreleased] — 2026-06-15 — Fix stale TestSearchFactsSemantic mocks (suite fully green)

The two long-failing `TestSearchFactsSemantic` tests had a stale collaborator mock:
`compute_embeddings_batch` now returns `(embeddings, tokens_used)` (the cost-telemetry
change), but the tests' `mock_compute` still returned just `[query_embedding]`, so the
tool's `results, _usage = await compute_embeddings_batch(...)` unpack failed. Updated all
three mocks to return the `(embeddings, 0)` tuple (assertions unchanged). Full suite now
1062 passed, 0 failed.

Also fixed the `PydanticDeprecatedSince211` warning (accessing `model_fields` on a model
*instance*) in `config.py` — use `type(settings).model_fields` (the class) at both sites.
This was nearly all the test-run warning noise: 216 warnings -> 1. (The remaining one is a
benign cross-test "coroutine never awaited" collected during a worker test.)

## [unreleased] — 2026-06-15 — Debugging tools: surface curator-worker telemetry + sweep diagnostics

- **scripts/proxy_status.py** (`metrics`): now surfaces the `curator_worker_diag` block
  (prepper fires/starved/cancels, hot-path LLM rate, briefing reads, avg lag,
  deterministic assemblies, sync top-up served/timeouts), `helper_tokens`
  (extractor/curator/embedding/cached), and `background_pass_successes` — previously
  none of the new telemetry was visible. Shows every non-zero assembly mode (not just 4)
  and trims the user-turns wall to the top 8 + a count.
- **scripts/prepper_sweep.py**: pre-flight counts the session's cached files and warns
  loudly when 0 (the prepper can't fetch -> the sweep under-represents the live
  file-fetch workload; this explains the earlier `files=0`). Outcome breakdown now
  distinguishes complete / no_result / timeout, and reports median + max latency.
- **scripts/diag_pipeline.py**: ported off the removed Neo4j-driver init to the active
  graph backend (ladybug) on a fresh temp DB (never touches/locks the live DB), using the
  backend's own session methods instead of the Neo4j-only `graph.session` path. Works
  end-to-end again (session create/find/touch/turn + extraction).

## [unreleased] — 2026-06-15 — Synchronous prepper top-up (flexible, off by default)

When no usable briefing exists on a curator-eligible user turn, optionally block on ONE
bounded prepper pass and retry the (cheap, deterministic) inline read on the fresh briefing,
instead of falling through to the expensive full curator loop. This guarantees the curator
delivers on eligible turns — the background worker becomes a best-effort prefetch and this is
the correctness net (the plan's "optional synchronous top-up"). Cost: prepper latency on miss
turns only (~4-7s with the converge prompt).

- **config.py**: `prepper_block_on_miss` (default off) + `prepper_block_budget_ms` (10s).
- **curator/pipeline.py**: in `curate_context`, after the briefing read and before the full
  loop, block on the registered prepper (bounded by `prepper_block_budget_ms`), cache the
  briefing, and serve the inline read. Flexible: respects `briefing_max_staleness` and works
  whether or not the background worker / background pass is enabled (calls the registered
  prepper directly, so a synchronous-only config is valid).
- **metrics**: `prepper_block_topups` / `prepper_block_timeouts`, surfaced in `curator_worker_diag`.
- **tests**: 3 tests (serves from fresh briefing, no-op when disabled, timeout records metric).

## [unreleased] — 2026-06-15 — Prepper converge prompt (cap tool calls) + lever-sweep tool

The prepper returned `no_result` ~half the time live (model kept fetching files and hit
max_iterations without emitting). Changed the prepper system prompt to converge: call AT
MOST 5-7 tools (score, then fetch only the 2-3 highest-relevance files), then emit — "a
focused briefing that is actually produced beats an exhaustive one that never finishes."

- **curator/prepper.py**: rule 7 of `PREPPER_SYSTEM_PROMPT` now caps tool calls and reserves
  the final response for the context block.
- **scripts/prepper_sweep.py** (new): offline lever sweep against a copy of the live graph DB,
  loading a real coherence tail from the session trace. Sweeps `prepper_max_iterations` x
  default/converge prompt and reports complete-rate / latency / tool calls / files. Finding:
  iteration count is not the binding constraint (all configs complete offline); the converge
  prompt is ~25-35% faster with fewer tool calls. (Caveat: the offline copy's file_cache yields
  files=0, so it does not fully reproduce the live file-fetch workload.)

## [unreleased] — 2026-06-15 — Fix prepper timeout (default budget 30s -> 60s)

Live validation of the worker + deterministic read showed the curator hot path never
consumed a briefing (`briefing_reads=0`): the background prepper **timed out** at the
default 30s/12-iteration budget (≈all passes timed out), so no briefing was ever cached
and `curate_context` always fell back to the full loop. Raising
`prepper_latency_budget_ms` to 60s eliminated the timeouts; the prepper now completes and
caches a briefing, which the deterministic read consumes. Confirmed live end-to-end:
worker fires → prepper completes → briefing cached → curator engages on the user turn →
`deterministic_assemblies` + `briefing_reads` increment with `hot_path_llm_calls` flat
(LLM off the hot path). The prepper is background (no hot-path latency pressure) and the
worker debounces + never cancels, so the longer budget is safe.

## [unreleased] — 2026-06-15 — Phase 2: deterministic LLM-free hot-path read

Removes the synchronous LLM call from the two_curator inline read. The prepper's
`SessionBriefing` already carries the typed pools, so the hot path can compose the
context block in pure code and fit it to a token budget — no LLM, no 3s race.

- **curator/deterministic_assembler.py** (new): `build_deterministic_context()` composes the
  `=== SECTION ===` block from the briefing's typed pools (goal/state/issues/verification/
  decisions/facts kept verbatim; `RELEVANT CODE` fills the remaining budget, fence-closed on
  truncation). `run_deterministic_assembler()` matches the inline_pass_fn signature, makes NO LLM
  call, and falls through (returns None) on an empty briefing.
- **config.py**: `assembler_deterministic` (default off), `assembler_token_budget` (6000).
- **curator/__init__.py**: `configure_curation_mode()` registers `run_deterministic_assembler` when
  `assembler_deterministic` + two_curator; otherwise the LLM `run_assembler` (unchanged).
- **curator/pipeline.py**: don't count `hot_path_llm_calls` when the deterministic read serves the turn.
- **metrics.py / routers/metrics_router.py**: `deterministic_assemblies` counter, surfaced in `curator_worker_diag`.
- **tests**: `test_deterministic_assembler.py` (10) + a two_curator deterministic-registration test.
  Full suite 1057 passed (2 pre-existing unrelated `TestSearchFactsSemantic` failures).

## [unreleased] — 2026-06-15 — Event-driven curator worker (Phase 0+1) + metrics/passthrough fixes

Branch `feat/event-driven-curator-worker`. Implements Phases 0-1 of the event-driven
curator-worker plan and fixes metric/passthrough recording gaps surfaced by live validation.

- **metrics.py**: Phase 0 counters `prepper_fires`, `prepper_starved`, `prepper_cancels`,
  `hot_path_llm_calls`, `hot_path_briefing_lag_{sum,count}`.
- **openai/extraction.py**: increment the Phase 0 counters at the starvation guard, the trigger,
  and (state.py) the cancel. **Phase 1**: when `curator_worker_enabled`, feed the event-driven
  worker on every turn boundary — DECOUPLED from the `is_user_turn/tool_calls` guard that starved
  the legacy prepper (the guard dropped agentic `finish_reason=stop` turns). Legacy path unchanged
  when the flag is off.
- **curator/worker.py** (new): long-lived per-session `CuratorWorker` + `WorkerRegistry` — idle-gated,
  debounced, no cancel-and-lose. Reuses `run_background_pass`. 15 unit tests in
  `tests/test_curator/test_worker.py`.
- **curator/pipeline.py**: Phase 0 hot-path LLM-call + briefing-lag instrumentation; count
  `background_pass_successes` in the two_curator/prepper path (previously only the default path).
- **curator/prepper.py**: record the background prepper's curator-model token spend into
  `curator_*_tokens_total` so two_curator cost is visible.
- **openai/chat.py**: call `record_assembly_mode()` for both curated and passthrough requests
  (was never called → `assembly_modes` always 0); record passthrough `total_input_tokens_seen`
  symmetrically so the A/B passthrough arm is recorded like a curated session.
- **routers/metrics_router.py**: surface `curator_worker_diag` (prepper starvation/cancel/hot-path
  LLM rates + briefing lag), `helper_tokens` (extractor/curator/embedding totals), and
  `background_pass_successes` on `/metrics`.
- **config.py**: `curator_worker_enabled` (default off), `curator_worker_debounce_ms`,
  `curator_worker_max_queue`, `curator_worker_idle_ttl_s`.
- **Live gate (archolith-bench context_quality_ab)**: worker fires reliably on agentic turns
  (`prepper_fires` climbs, `prepper_starved=0`, `prepper_cancels=0`), the prepper pass completes and
  caches briefings, and the hot path consumes one (`briefing_reads=1`, lag 1.0 fresh). Full suite
  1046 passed (2 pre-existing unrelated `TestSearchFactsSemantic` failures).

## [unreleased] — 2026-06-13 — Helper-LLM prompt-cache hit tokens on TurnTrace

Thread `cached_tokens` (OpenAI `prompt_tokens_details.cached_tokens`) through the curator
and extractor telemetry paths and record them on `TurnTrace`.

- **curator/loop.py**: accumulate `curator_cached_tokens` across iterations via `getattr`-safe
  `response.usage.prompt_tokens_details.cached_tokens`; default 0 when field absent.
- **curator/result.py**: `CuratorResult.cached_tokens_used: int = 0`.
- **curator/pipeline.py**: pass `curator_cached_tokens=result.cached_tokens_used` in both
  `AssembledContext` construction sites.
- **models/dtos.py**: `AssembledContext.curator_cached_tokens: int = 0`;
  `TurnTrace.extractor_cached_tokens: int = 0` + `TurnTrace.curator_cached_tokens: int = 0`.
- **extractor/client.py**: single-call path captures `cached_tokens` from
  `prompt_tokens_details`; accumulator init includes `"cached_tokens": 0`; per-tool merge
  loop and turn-level capture both accumulate it.
- **openai/extraction.py**: `extractor_cached_tokens` threaded into `set_helper_usage`;
  `record_metric("extractor_cached_tokens_total", ...)`.
- **openai/chat.py**: `curator_cached_tokens` threaded into `set_helper_usage`;
  `record_metric("curator_cached_tokens_total", ...)`.
- **trace/builder.py**: `set_helper_usage` gains `extractor_cached_tokens` and
  `curator_cached_tokens` params; both written to `_data` via existing `if value:` pattern.
- **metrics.py**: counters `extractor_cached_tokens_total` and `curator_cached_tokens_total`
  initialised to 0.
- **tests/test_helper_cached_tokens.py**: new test file; 13 tests covering JSONL round-trip,
  `set_helper_usage`, curator accumulation (with and without `prompt_tokens_details`), and
  extractor usage dict handling.

## [unreleased] — 2026-06-12 — Coherence-tail integrity + prefetch workspace default

Tail-handling correctness fixes from the 2026-06-09 review, plus a prefetch
security default.

### Correctness
- **rewrite**: `_validate_tail` is now tool-call-aware. A leading `assistant(tool_calls)` whose tool results follow within the tail is kept intact (previously the leading-strip loop discarded exactly the expansion `smart_tail` performed, silently restarting the curated tail at the last user message). Truly orphaned leading `tool` messages are dropped one at a time; plain leading assistants are still dropped conservatively. The same-role merge no longer merges across a `tool_calls` boundary (which silently dropped the second message's tool_calls). (`archolith_proxy/proxy/rewrite.py`)
- **rewrite (F10)**: removed the duplicate user-first comprehension that used `result.index(m)` (misbehaves on duplicate messages, 2026-06-07 audit F10); `_ensure_user_first()` is now the sole authority. (`archolith_proxy/proxy/rewrite.py`)
- **tail**: when expanding the coherence tail would exceed `max_size`, `smart_tail` now truncates at a turn boundary (first `user` message in the last `max_size` window, else past leading orphaned `tool` messages) instead of returning the naive fixed slice that reintroduced orphaned-tool 400s. Warning detail changed to `fallback="turn_boundary"`. (`archolith_proxy/assembler/tail.py`)

### Security
- **curator**: new `prefetch_restrict_to_workspace` (default True). When `prefetch_allowed_roots` is empty, `prefetch_file` resolves the session `working_directory` (+ `workspace_root`) from the trace-store `harness_env` metadata and denies reads outside it; no workspace on record denies prefetch entirely. Explicit `prefetch_allowed_roots` keeps precedence; set the flag False to restore legacy unrestricted reads. Closes a prompt-injection path that could read arbitrary host files into upstream prompts. (`archolith_proxy/config.py`, `archolith_proxy/curator/tools.py`)

### Tests
- New: `tests/test_tail_validation.py`, `tests/test_assembler/test_tail_fallback.py`, `tests/test_curator/test_prefetch_workspace.py`. Full suite: 1020 passed (2 pre-existing `TestSearchFactsSemantic` failures unrelated to this change).

---

## [unreleased] — 2026-06-09 — Deferred hardening (design risks D1-D5, D7-D10)

Final remediation pass from the 2026-06-09 full-project audit. Closes every
remaining confirmed design risk (D6 was handled in the dedup pass below).

### Security / robustness
- **admin (D1)**: an empty `ADMIN_TOKEN` now opens admin endpoints only to loopback peers (`127.0.0.0/8`, `::1`, `::ffff:127.0.0.1`); non-loopback peers get 401. New `ADMIN_ALLOW_OPEN_NONLOCAL` (default False) is an explicit escape hatch. **Behavior change:** exposed (non-localhost) deployments must now set `ADMIN_TOKEN` or the escape hatch. (`archolith_proxy/admin.py`, `config.py`)
- **memory (D7)**: `generic_http` adapter `validate_config` rejects a `base_url` that is not http(s) or has no host. (`archolith_proxy/memory/adapters/generic_http.py`)
- **startup (D4)**: a configured graph backend that fails to initialize is now reported as `degraded` on `/health` (HTTP 503) with the reason, instead of an indistinguishable `not_configured`/200. New `REQUIRE_GRAPH_ON_STARTUP` (default False) aborts startup instead of serving silently degraded. (`archolith_proxy/main.py`, `config.py`)

### Correctness
- **graph (D2)**: file-cache recall resolves an ambiguous suffix match deterministically (stable secondary sort, lexicographically-smallest) and still warns, instead of returning `None` — which assembly could not distinguish from a cache miss (file context silently dropped). (`archolith_proxy/graph/ladybug_files.py`)
- **streaming (D8)**: `ResponseCapture` preserves `tool_calls` from a non-streaming recall re-send (new `.tool_calls`); the streaming finalize path falls back to a tool_call summary so a tool-call-only final message still produces extraction input. (`archolith_proxy/proxy/streaming.py`, `openai/streaming.py`) NOTE: wiring tool_calls directly into the extraction message list needs `openai/extraction.py` (out of scope) — follow-up.
- **streaming (D3)**: the recall decision timeout is configurable via `STREAMING_RECALL_DECISION_TIMEOUT_S` (default 5.0) and a recall sentinel arriving after the window logs `streaming_recall_sentinel_after_timeout` so the bypass is observable. Full correctness (dynamic buffering) remains a deferred follow-up. (`archolith_proxy/proxy/streaming.py`, `openai/streaming.py`, `config.py`)

### Durability / performance / observability
- **memory (D5)**: optional JSONL persistence for the promotion audit trail via `PROMOTION_AUDIT_DIR` (best-effort; unset keeps in-memory-only behavior). (`archolith_proxy/memory/promotion.py`, `config.py`)
- **trace (D9)**: session LRU eviction is O(1) via `OrderedDict` (was O(n) `list.remove()` per touch); eviction semantics unchanged. (`archolith_proxy/trace/store.py`)
- **metrics (D10)**: `/metrics` computes all trace-derived metrics (user turns, token/cost totals, curator latencies) under a single lock acquisition for one consistent snapshot, instead of three separate locked scans. (`archolith_proxy/routers/metrics_router.py`)

### Tests
- New: `test_admin_loopback_guard.py`, `test_generic_http_validate.py`, `test_health_degraded.py`, `test_graph/test_file_cache_ambiguity.py`, `test_proxy/test_streaming_tool_calls_capture.py`, `test_proxy/test_streaming_recall_late_sentinel.py`, `test_memory_promotion_audit.py`, `test_trace/test_lru_eviction_order.py`, `test_metrics_consistency.py`. Full suite green (953 passed).

---

## [unreleased] — 2026-06-09 — Dedup + cheap hardening (defects #3, #4, #5, D6)

### Fixed
- **extraction**: dedup now checks **all** session facts via a content-hash set, not a recency-bounded window. `get_active_facts(limit=fact_pool_limit)` only compared the most recent facts, so semantic duplicates of older facts re-entered the graph; the new path compares against `get_all_fact_hashes(session_id)` (every active fact). The `fact_pool_at_capacity` warning is removed (no longer applicable). Trade-off: exact (post-normalization) content duplicates are caught across the full pool, but near-duplicate (Jaccard) matching against the stored pool no longer applies — within-extraction near-duplicate collapsing (`deduplicate_facts` in the per-tool merge) is unchanged. (`archolith_proxy/openai/extraction.py`, `archolith_proxy/extractor/dedup.py`, `archolith_proxy/graph/ladybug_backend.py`, defect #3 from 2026-06-09 audit)
- **models**: widen `PromotionRecord.compute_dedupe_key` from 64 to 128 bits (`hexdigest()[:16]` → `[:32]`), eliminating the practical collision risk that could silently drop a distinct fact. (`archolith_proxy/memory/models.py`, defect #4)
  NOTE: existing 16-char dedupe keys in the DB are incompatible with new 32-char keys. On upgrade, facts promoted before this fix may not dedup against post-fix keys for the same content — expect a one-time, bounded batch of near-duplicate promotions that subsequent dedup passes filter. Benign.
- **backend**: WAL rotation "depth exceeded" error now reports the rotation depth at time of failure instead of the just-reset counter (was always "after 0 rotation attempts"). (`archolith_proxy/graph/ladybug_backend.py`, defect #5, cosmetic)
- **extractor**: per-tool merge guards against `facts=None`/`files_touched=None` from any extractor (`r.facts or []`), preventing `TypeError` on `list.extend(None)`. (`archolith_proxy/extractor/client.py`, design risk D6)

### Tests
- `tests/test_extractor/test_dedup.py` — `_fact_content_hash` + `deduplicate_facts_by_hash`: beyond-recency-window duplicate rejected, cross-session content kept, within-batch exact-dup collapse, content-only hashing.
- `tests/test_extractor/test_per_tool_extraction.py` — extractor returning `facts=None` no longer crashes the merge.

---

## [unreleased] — 2026-06-09 — Concurrency correctness (defects #1 and #2)

### Fixed
- **extraction**: fail closed (return early) when session lock acquire times out — prevents unserialized file-cache, dedup, and graph writes when a competing turn holds the session lock. (`archolith_proxy/openai/extraction.py`, defect #1 from 2026-06-09 audit)
- **sessions**: serialize `find_or_create_by_fingerprint` per fingerprint with a double-checked `asyncio.Lock` — prevents duplicate Session nodes when two concurrent first requests for the same client fingerprint both observe no existing session. (`archolith_proxy/graph/ladybug_sessions.py`, defect #2 from 2026-06-09 audit)

### Tests
- `tests/test_extraction_concurrency.py` — two new concurrency tests written before fixes to prove both defects, confirm they pass after.

---

## 2026-06-07 — Deep RTK → filter rename (DTO + trace builder + dashboard)

Complete nomenclature unification across trace DTO, builder, logs, and dashboard:
- **DTO field rename with back-compat:** `TurnTrace` fields `rtk_*` → `filter_*` (available, chars_saved, chars_before, chars_after, strategy_savings, latency_ms). Pydantic v2 `AliasChoices` + `populate_by_name=True` allow old persisted traces (.jsonl with `rtk_*` keys) to deserialize correctly; output normalizes to `filter_*` keys.
- **Trace builder methods:** `set_rtk_latency()` → `set_filter_latency()`, `set_rtk_stats()` → `set_filter_stats()`. Internal dict keys updated to match new field names. Updated callers in `openai/chat.py` and refactored `proxy/agent_solo.py` (variable rename: `rtk_stats` → `filter_stats`).
- **Logging keys:** filter_adapter.py event names: `rtk_dependency_missing` → `filter_dependency_missing`, `rtk_filter_failed` → `filter_failed`, `rtk_filter_single_failed` → `filter_single_failed`, `rtk_shrink_args_failed` → `filter_shrink_args_failed`, `rtk_shrink_tail_failed` → `filter_shrink_tail_failed`.
- **Dashboard JS:** Variable names and display labels updated (rtkBadge → filterBadge, rtkSavingsStr → filterSavingsStr, rtkStrategyStr → filterStrategyStr). Added fallback chains (`t.filter_available ?? t.rtk_available`) for safe transition.
- **Tests:** Renamed `test_rtk_filtering.py` → `test_filter_adapter.py`; updated assertions and method calls throughout (set_filter_stats, filter_available, filter_chars_*). Added back-compat regression test `test_back_compat_rtk_field_aliases()`.
- **.env.example:** Canonical `FILTER_ENABLED=false`, deprecated `RTK_ENABLED=false` aliased variant documented.

## 2026-06-07 — Chunk 7: Cross-cutting hygiene and version metadata

Port and version standardization, docker/pyproject hardening, optional javalang:
- **Port alignment:** All scripts default to 9800 (canonical port matching config.py and docker-compose.yml). Deployment must migrate from 9801 to 9800.
- **Version metadata:** `__version__` now read from installed package (pyproject 0.3.0) via `importlib.metadata`, with "0.0.0-dev" fallback. Replaces hardcoded "0.1.0" in main.py health endpoints + metrics_router.py.
- **Dockerfile:** Copies `uv.lock` for reproducible builds; installs production deps only (no dev); removes test/ from runtime image.
- **pyproject.toml:** Removed `httpx` duplication from dev deps (already in main). Moved `javalang` to optional `[project.optional-dependencies]` `java = [...]`.
- **text_utils.py:** `_build_outline()` now catches `ImportError` for missing javalang, gracefully falling back to regex-based outline.
- **Trace retention:** Added `trace_retention_days` config setting (default 0 = no cleanup); TraceStore now runs cleanup on startup to delete JSONL files older than the retention window.
- **gitignore:** Added `.ruff_cache/` and `config_overrides.json` (runtime override file, not for repo).
- **scripts/README.md:** Documented missing scripts (scripted_benchmark.py, harness_benchmark.py, test_synthetic_tools.py, redundancy.py, opencode_export.py).
- **docker-compose.yml:** Added EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL env vars.

## 2026-06-05 — Fix: proxy was inert on real sessions (RTK missing + trace mislabel)

Root-caused via replaying a real captured coding session: the proxy did NO context
management on real agent sessions (100% passthrough), which produced the 58/100 grade.
- **Root cause (env):** `archolith_rtk` was installed only in the global Python, not in
  the proxy's `.venv` (which `proxy_restart.py` launches). Agent-solo compression is RTK
  code, so it silently no-op'd and `rtk_available` was false. Fixed by installing RTK into
  the venv; after the fix a real session compresses ~190K chars/turn (filter+dedup+shrink).
- `main.py`: **refuse to start** (raise `RuntimeError`) when `RTK_ENABLED=true` but
  `archolith_rtk` is not importable — a proxy that silently does no curation is worse than
  one that won't boot. (`rtk_enabled` defaults to false, so tests are unaffected.)
- `openai/chat.py`: **`set_assembly()` is now called on the normal request path.** It only
  ran in the `-passthrough` branch before, so every normal request recorded
  `assembly_mode="passthrough"` with 0 savings even when agent-solo/curator compressed
  heavily — the reason every baseline looked 100% passthrough. Traces now report the real
  mode and savings.
- `scripts/benchmark.py`: pin the proxy session via `X-Session-ID` (`send_chat` gains a
  `session_id` arg); trace lookups no longer guess `sessions[0]` and grab a stale
  disk-restored session.

## 2026-06-05 — Proxy memory-leak fixes (recoverable on session resume)

Investigated unbounded in-memory growth before running the tuning baseline. Fixes below;
all evicted/pruned state is recoverable — caches rebuild from the graph or on the next turn.
- `trace/store.py`: `_bg_passes` is now capped per session (`max_bg_passes_per_session`, default 50) like turns were, and session LRU eviction now drops `_bg_passes` + `_session_meta` (previously only turns were dropped, leaking both for every evicted session). Added `has_session_metadata()`.
- `openai/chat.py`: per-session trace metadata (harness_env, proxy_config) now repopulates whenever absent — not only on `is_new` — so a session resuming after an LRU eviction restores its metadata instead of losing it for the process lifetime.
- `main.py`: the in-memory cache prune (curator + agent-solo + last-attempts) now runs every cleanup cycle, not only when a graph session expired — previously the prune was nested under `if expired:` and could be skipped indefinitely.
- `curator/pipeline.py`: added `prune_last_attempts()` for the `_last_attempt` diagnostic map (regenerated per curator run).
- `proxy/agent_solo.py`: `_curator_caches` (holds full rewritten message lists) now has a hard cap mirroring `_session_trackers`, as defense-in-depth between prune cycles.
- `tests/test_memory_bounds.py`: regression tests for the bg-pass cap, eviction cleanup, resume recoverability, last-attempt prune, and curator-cache cap.

## 2026-06-05 — RTK / curator tuning: Step 0 offline harness extensions

- `scripts/redundancy.py`: offline read-file redundancy analyzer — classifies file-read tokens in a captured session into exact-dup / superseded-by-full-write / live buckets to size RTK Step 5-B and curator Step 4-C before building them. Partial edits do not count as superseding. 6 unit tests.
- `scripts/benchmark.py`: added `EditProbe` + pure `score_edit_probe` + `run_edit_probes` — an edit-fidelity probe alongside the keyword `FactProbe`. Fidelity = fraction of required fragments present, 0.0 if any forbidden (stale) fragment appears. Reports `avg_*_fidelity`, `fidelity_preservation`, `proxy_forbidden_hits`; wired into scenario loading, checkpoint/resume, summary, and print output. 7 unit tests.
- `.agent/workflows/benchmarking.md`: documented both tools and the determinism decision — variance-based (N>=3 runs, compare medians/spread) rather than a byte-replay cache, since the harness makes live temperature-0.3 calls.

## 2026-06-04 — RTK / curator tuning: instrumentation, proxy recall, and repeated-call detection (Steps 1–4)

### Step 1 — Baseline instrumentation
- Added `rtk_available`, `rtk_chars_saved`, `rtk_chars_before` to `TurnTrace` — records whether archolith_rtk is installed and how many chars the filter removed per turn.
- Added `curator_skip_reason` to `TurnTrace` — classifies why the curator was eligible but did not assemble context (`cold_start`, `disabled`, `inline_timeout`, `no_result`, `timeout`, `exception:…`).
- Added `is_available()` to `rtk.py` — distinguishes fail-open (package missing) from active RTK.
- Added `set_rtk_stats()` and `set_curator_skip_reason()` to `TraceBuilder`.
- Dashboard: RTK✓/RTK✗ badge on turn header; rtk savings in token line; curator skip reason label on passthrough user turns.

### Step 2 — Config experiments
- `AGENT_SOLO_MIN_INPUT_TOKENS=3000` in `.env` (lowered from default 8000 to compress nearly all multi-turn coding sessions from turn 2 onward).
- Added "Tuning experiments (2026-06-04)" section to `.agent/workflows/benchmarking.md` with four named variants (A–D): solo threshold, briefing staleness, synthetic tools off, background pass comparison.

### Step 3 — Proxy-driven recall
- Added `detect_recall_trigger()` to `proxy/recall.py` — fires on explicit recall language in the last user message (`user_phrase`) or when the same file appears in ≥2 tool results in recent messages (`repeated_file_read`).
- Added `inject_proxy_recall_into_body()` — prepends a `[PROXY-RECALL | trigger=…]` block to the system message before upstream dispatch.
- `chat.py`: proxy-forced recall block runs between RTK filter and synthetic tools injection; logs `proxy_recall_injections` metric.
- Added `recall_trigger` field to `TurnTrace` (`"proxy_forced:<type>"` | `"model_invoked"` | `""`).
- Extended `TraceBuilder.set_recall()` with `trigger` kwarg.
- Dashboard: recall line shows `[trigger]` annotation.

### Step 4 — Curator tightening
- `curator/loop.py`: repeated `get_file`/`get_file_lines`/`prefetch_file` calls for the same path now append a `PROXY-NOTE` to the result discouraging re-fetch; repeated `search_facts`/`search_facts_semantic` calls for the same query similarly noted. Added `_seen_queries` set per run.
- `curator/pipeline.py`: `_run_with_briefing` now populates `_last_attempt` on inline timeout, exception, and no_result — inline pass failures now reach the dashboard's `curator_skip_reason`.
- `curator/prompts.py`: `_format_previous_snapshot` now emits a `PROHIBITED` prefix with an explicit list of banned tool calls for already-fetched file paths. Delta guidance tightened to "Re-fetching costs an iteration and produces no benefit."

### Post-review remediation
- `set_recall()` now records `model_invoked` explicitly when recall happened without a proxy-forced trigger.
- `TurnTrace` now records `rtk_chars_after`, `proxy_recall_chars_added`, `outbound_chars_sent`, and `rtk_strategy_savings` so RTK filter savings can be compared against final outbound payload size after proxy recall injection.
- Dashboard turn cards now render RTK strategy-level savings and final outbound char counts, including proxy-recall additions.
- Curator tool-log records now preserve `proxy_note` warnings for repeated file reads and repeated fact searches, and the dashboard renders those notes directly.
- Added targeted regression coverage for recall-trigger labeling, RTK/outbound trace stats, strategy breakdown persistence, and curator proxy-note serialization.

## 2026-06-01 — Briefing enabler: pluggable curation mode registration

- Added `SessionBriefing.mode` field (`"two_pass"` | `"two_curator"`) — tags which curation mode produced a briefing.
- Added `register_curation_mode()` / `unregister_curation_mode()` to `curator/__init__.py` — lets the two-curator mode swap in its own prepper/assembler functions while the single-bot two-pass mode remains the default fallback.
- Refactored `run_background_pass()` to dispatch to a registered background pass function when present, falling back to `_run_background_pass_inner()` otherwise.
- Refactored `curate_context()` to dispatch to a registered inline pass function for briefing-assisted passes when present, falling back to `_run_with_briefing()` otherwise.
- Added `briefing_max_staleness` config key (default 2) — replaces the hardcoded `turn_number - 2` threshold in briefings staleness check.
- Added `CuratorResult.assembly_mode` field (default `"curator"`) — enables mode-specific results to self-identify their assembly path.
- 18 new tests: registration hooks (6), background pass dispatch (3), inline pass dispatch (3), briefing mode field (3), CuratorResult assembly_mode (3). All 54 tests pass.
- Full backward compatibility — single-bot two-pass behavior is unchanged when no mode is registered.

## 2026-05-30 — Two-pass curator: background pre-fetch + inline briefing

- Added two-pass curator architecture (disabled by default, `BACKGROUND_PASS_ENABLED=true`).
  - **Background pass:** after each upstream response, an async curator loop runs with up to `BACKGROUND_PASS_MAX_ITERATIONS` (default 12) tool calls and caches a `SessionBriefing` for the next turn.
  - **Inline pass:** on the next request, if a fresh briefing is available, the curator runs with only 2 iterations using the pre-fetched briefing (files, outlines, key facts) instead of re-discovering from scratch.
  - Fallback: if no briefing exists or the briefing is stale (`source_turn < turn_number - 2`), falls through to the standard full curator run.
- Added `raw_result` field to `CuratorToolCall` — stores the full tool result text so the briefing builder can use complete file contents instead of the 200-char preview. Excluded from `to_dict()` to keep traces bounded.
- Added `background_pass_latency_budget_ms` config (default 30000ms) — `asyncio.wait_for` timeout guard on `run_background_pass()`. On timeout, logs and returns silently without blocking the response.
- Added `background_pass_debounce_ms` config (default 2000ms) — minimum interval between background passes to avoid thrashing on rapid turns.
- Dashboard: `modeTag()` now handles `briefing` and `briefing_stale` assembly modes.
- 36 new tests (9 integration + 27 unit): background pass pipeline, inline briefing pipeline, raw-result fidelity, env-var config overrides.

## 2026-05-30 — License, CLA, and Benchmark Refresh

- Switched license from Apache 2.0 to PolyForm Noncommercial 1.0.0 (consistent with archolith-bench and archolith-rtk).
- Added CLA.md (Contributor License Agreement) and .github/pull_request_template.md with CLA checkbox.
- Updated CONTRIBUTING.md with CLA reference.
- Updated README benchmark section with archolith-bench headline numbers (58.6% proxy, 50% filter, 71.5% MCP waste).
- Added archolith trademark notice to README.
- Updated pyproject.toml: license, author email, repository URLs, benchmarks link.

## 0.3.0 — 2026-05-30 — SSE fix, circuit breaker, token budget

- **CRITICAL FIX:** `_wrap_response_as_sse()` now emits `tool_calls` deltas with proper `index` keys when converting non-streaming responses to SSE. Previously only emitted `role`, `content`, and `finish_reason` — any response with `finish_reason: "tool_calls"` but no tool call data caused OpenCode to error/retry infinitely, burning tokens until compaction killed the session.
- Added per-session circuit breaker for synthetic tool re-injection: after 3 consecutive failures, synthetic injection is disabled for 5 minutes; after 10 total failures, disabled for session lifetime.
- Added per-session token budget (`MAX_INPUT_TOKENS_PER_SESSION`, default 2M) with configurable action (`SESSION_TOKEN_BUDGET_ACTION`: "passthrough" or "reject").
- Added synthetic tool metrics to `/metrics`: `synthetic_tool_successes`, `synthetic_tool_failures`, `synthetic_circuit_opens`, `synthetic_circuit_hard_disables`, `synthetic_injections_skipped`, `synthetic_circuit_states`.
- Improved synthetic tool fallback message to redirect the agent to use file tools directly instead of leaving it confused.
- Added `SyntheticResult.fallback_used` flag to distinguish fallback-stripped responses from successful re-sends (enables circuit breaker feedback).
- Added 18 unit tests for SSE tool_calls conversion, circuit breaker, and token budget.
- Updated `.agent/architecture.md` to document `archolith-context` as the new project name, the Curator LLM subsystem (entry point, loop, 7 tools, result type), File Content Cache (FileContent schema, SHA-256 dedup pipeline), updated data flow with curator-then-heuristic path, new env vars (`CURATOR_*`, `FILE_CACHE_*`), and curator metrics counters. Removed RTK references (RTK belongs in `archolith-rtk`).

## 2026-05-23 — Phases 1–4: File Content Cache + LLM-driven Curator

- Added `archolith_proxy/curator/` package: `CuratorResult` dataclass, 7 async curator tools, OpenAI-compatible tool schemas, system prompt, LLM loop (`_run_curator_native` + Nous XML fallback, ported from cth.mcp.delegate), `curate_context()` entry point with `asyncio.wait_for` 6s hard cap and heuristic fallback.
- Added `FileContent` LadybugDB node table with SHA-256 dedup; `_extract_file_reads()` pairs file-read tool results to calls via `tool_call_id`; `_upsert_file_cache()` called inside `_run_extraction()` before message flattening.
- Wired curator as primary assembly path in `chat.py`; heuristic assembler used as fallback when curator returns `None` or is disabled.
- Added `FILE_CACHE_ENABLED`, `FILE_CACHE_MAX_FILE_BYTES`, `CURATOR_*` settings to `config.py`.
- Added `curator_calls`, `curator_timeouts`, `curator_fallbacks`, `assembly_modes["curator"]` to `metrics.py`.
- Fixed streaming trace finalization so `facts_stored`, `extracted_facts`, and `upstream_response_summary` are recorded after post-response extraction instead of being frozen at zero before the background task runs.
- Fixed extraction input shaping to normalize structured content blocks and prioritize the newest tool outputs within the 4K extraction budget instead of truncating from the oldest tool results first.
- Added `extraction_empties` to `/metrics` and stopped counting zero-fact parses as successful extractions.
- Fixed `/trace/qa/extract` dedup and invalidation diagnostics to use the active graph backend instead of Neo4j-only helpers.
- Added regression coverage for streaming trace ordering, recent-tool extraction prompts, and empty-extraction metric semantics.
- Fixed recall tool formatting so `__archolith_recall` now emits string `role="tool"` content instead of serializing the `(text, compression_ratio)` tuple from `_format_relevant_facts()`.
- Fixed compaction re-write flow so compacted context updates the actual outbound payload, not just `AssembledContext.system_message`.
- Fixed skipped-rewrite trace accounting so `rewritten_messages`, `rewritten_tokens`, and `savings_tokens` reflect the payload actually sent upstream.
- Added regression coverage for recall formatting, compaction-aware rewriting, skipped-low-tokens trace accounting, and the streaming recall test warning.
- Updated project architecture docs to point at the canonical `archolith_proxy/` package tree and current bootstrap defaults.
- Added optional Phase 4 RTK integration for outbound tool-role messages via `RTK_ENABLED` and a fail-open proxy adapter.
- Applied RTK filtering to the primary upstream request path plus recall re-send paths so surviving tool results are filtered consistently.
- Added proxy coverage for RTK-enabled and RTK-disabled outbound payload behavior and documented the new configuration surface.
