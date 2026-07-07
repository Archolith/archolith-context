# Deep Code Audit: Chunk 1 — archolith_proxy/proxy/ + archolith_proxy/openai/

**Auditor:** opencode (z-ai/glm-5.1)
**Date:** 2026-06-07
**Scope:** `archolith_proxy/proxy/` (13 files), `archolith_proxy/openai/` (11 files)
**Total LOC:** ~4,800

---

## Summary

The chunk implements the core request path: FastAPI route handling, SSE streaming passthrough with recall interception, session resolution, message rewriting, circuit breaking, and synthetic tool injection. The architecture is sound — clean separation of concerns, deferred imports to avoid cycles, bounded caches with eviction, and fail-open peer integration. No Critical findings. Three High-severity issues concern correctness edge cases (DSML over-stripping, content-encoding passthrough, streaming recall + tool_calls coexistence). The import DAG is acyclic. Two of three prior audit concerns are RESOLVED; one is partially improved but retains a theoretical risk.

---

## Findings

| # | File:Line | Severity | Category | Status | Description |
|---|-----------|----------|----------|--------|-------------|
| F1 | `proxy/rewrite.py:43` | **High** | Correctness | **STILL EXISTS** | `_DSML_BLOCK_RE` strips from first DSML marker to **end of string** (`\Z` via `re.DOTALL`). If a false-positive match occurs (rare unicode in prose), all subsequent content is silently eaten. The bounded `_DEEPSEEK_TOOL_BLOCK_RE` and `_NOUS_TOOL_CALL_RE` are safe — only `_DSML_BLOCK_RE` is greedy-to-EOF. |
| F2 | `openai/passthrough.py:20` | **High** | Correctness | **NEW** | `content-encoding` is in `HOP_BY_HOP_HEADERS`, stripped from both request and response headers. If upstream returns a gzip-compressed body with `content-encoding: gzip`, the proxy removes the header but passes the compressed bytes through, causing the client to receive garbage (double-encoding if client re-compresses, or binary if client expects plaintext). `content-length` removal is correct for streaming; `content-encoding` removal is not. |
| F3 | `proxy/streaming.py:596-604` | **High** | Correctness | **STILL EXISTS** | `stream_with_recall_detection` decides passthrough when `first_tool_name != recall_tool_name`. If the model calls a non-recall tool **first** (index 0) but calls `__archolith_recall` as a **secondary** tool (index 1+), the recall is not intercepted. The code comment acknowledges this ("we don't intercept mid-stream for that case") but the streaming recall path is completely skipped, producing a response with an unresolved `__archolith_recall` tool call exposed to the client. Non-streaming path handles this via `find_recall_tool_call` which checks all tool_calls. |
| F4 | `proxy/agent_solo.py:96` | **Medium** | Security | **STILL EXISTS** | `_fingerprint_message` uses `hashlib.md5()` — not a security vulnerability per se (collision preimage is infeasible for fingerprint use), but md5 is deprecated for any cryptographic purpose and signals poor practice. SHA-256 is already used in `session.py:163` for the same class of operation. |
| F5 | `proxy/locks.py:31` | **Medium** | Performance | **IMPROVED** | `_session_locks` dict is unbounded in normal flow — `cleanup_stale_locks` only runs when called externally (no automatic trigger). `cleanup_session_lock` requires the caller to know the session ID. Unlike `circuit_breaker.py` which auto-evicts at 10K via OrderedDict LRU, `_session_locks` relies on an external prune cycle. If prune_session_state or cleanup_stale_locks is not called regularly, the dict grows without bound. The `max_locks=10000` threshold exists but is never automatically checked. |
| F6 | `proxy/session.py:49-50,68-69` | **Medium** | Correctness | **STILL EXISTS** | `set_benchmark_session_id` / `set_benchmark_passthrough_session_id` use `global` mutation of module-level vars. In a multi-threaded deployment (e.g., uvicorn with `--workers > 1` using threads), these are not thread-safe. Currently safe because the proxy runs single-event-loop, but fragile if deployment changes. |
| F7 | `proxy/circuit_breaker.py:48` | **Medium** | Performance | **STILL EXISTS** | Uses `threading.RLock` in an async application. The lock is held during quick dict operations (~microseconds), so blocking the event loop is negligible in practice. However, `threading.RLock` is technically a blocking primitive in async context — if any mutation path ever becomes I/O-bound (e.g., logging to a remote sink inside the lock), it would stall the event loop. An `asyncio.Lock` would be more idiomatic. |
| F8 | `proxy/synthetic_tools.py:378-402` | **Medium** | Correctness | **STILL EXISTS** | `_fallback_strip_synthetic` silently strips tool calls from the model's response when re-send fails. If the model called only synthetic tools, it injects a hardcoded English message as `content`. This is documented behavior but causes silent data loss — the model's actual text content (if any existed before the synthetic tool call) is discarded. The function only preserves content if it exists alongside tool_calls. |
| F9 | `proxy/streaming.py:327-428` | **Low** | Maintainability | **RESOLVED** | `_non_streaming_to_sse` and `_wrap_response_as_sse` correctly emit tool_calls deltas with proper OpenAI streaming format: first delta has `{index, id, type, function: {name, arguments: ""}}`, subsequent delta has `{index, function: {arguments: "..."}}`, final delta has `finish_reason`. The prior audit concern about tool_calls delta drops is **RESOLVED**. |
| F10 | `proxy/rewrite.py:274-278` | **Low** | Maintainability | **NEW** | `_ensure_user_first` is called after a complex inline filtering in `rewrite_messages` that already tries to ensure user-first ordering. The inline filter (lines 274-278) is convoluted with `result.index(m)` inside a list comprehension that could produce wrong indices if duplicates exist. The `_ensure_user_first` helper at line 680 is cleaner. The inline filter is dead code if `_ensure_user_first` is always called, but both run. |
| F11 | `proxy/agent_solo.py:84` | **Low** | Performance | **IMPROVED** | `_curator_caches` now has a `_MAX_SESSIONS=200` cap (added since prior audit), matching `_session_trackers`. Prior audit flagged unbounded growth — **RESOLVED** with the cap. However, each `_CuratorCache` stores a full `rewritten` message list (shallow copy), which can be large. No per-entry size limit. |
| F12 | `openai/chat.py` | **Low** | Maintainability | **STILL EXISTS** | Re-exports from helpers, extraction, file_cache with `# noqa: F401` — intentional for API surface, but creates import coupling. Minor. |

---

## Import DAG Analysis

### Edges (top-level imports only)

```
openai/chat.py → {openai/streaming, openai/non_streaming, openai/extraction, openai/file_cache, openai/helpers}
openai/streaming.py → {openai/extraction, proxy/streaming, proxy/live, proxy/upstream, config, metrics, filter_adapter, trace/*}
openai/non_streaming.py → {proxy/upstream, proxy/live, config, metrics, filter_adapter, trace/*}
openai/extraction.py → {openai/helpers, proxy/rewrite, config, graph/backend, trace/*}
openai/file_cache.py → {openai/helpers, config, graph/backend, trace/*}
openai/helpers.py → {config, trace/*}  (no sibling edges)
openai/passthrough.py → {config}
openai/models.py → {config, proxy/upstream}
openai/router.py → {openai/chat, openai/passthrough, openai/models}
openai/errors.py → {}  (leaf)
openai/schemas.py → {}  (leaf — pydantic only)

proxy/streaming.py → {proxy/upstream (via _wrap_response_as_sse import path)}  (actually: no direct import of upstream in streaming.py — only openai/streaming imports upstream)
proxy/locks.py → {}  (leaf)
proxy/circuit_breaker.py → {}  (leaf, metrics imported inside functions)
proxy/agent_solo.py → {}  (leaf, archolith_filter imported inside functions)
proxy/rewrite.py → {filter_adapter}
proxy/session.py → {graph/backend, trace/store}
proxy/synthetic_tools.py → {graph/backend, trace/store, proxy/upstream (deferred), filter_adapter (deferred), config (deferred)}
proxy/tool_injection.py → {graph/backend (deferred)}
proxy/recall.py → {proxy/tool_injection, graph/backend (deferred), trace/store (deferred)}
proxy/tool_intercept.py → {graph/backend (deferred)}
proxy/upstream.py → {config, metrics}
proxy/live.py → {}  (leaf)
```

### Deferred imports (inside functions)

| File | Deferred Import | Line | Reason |
|------|----------------|------|--------|
| `openai/extraction.py` | `proxy/locks` | ~47 | Avoid potential cycle |
| `openai/streaming.py` | `proxy/tool_injection` | 187-194 | Only needed in recall path |
| `proxy/synthetic_tools.py` | `proxy/upstream`, `filter_adapter`, `config` | 430-432 | Only needed in re-send path |
| `proxy/agent_solo.py` | `archolith_filter.dedupe` | 46 | Optional peer, fail-open |

### Cycle detection

**No cycles found.** The DAG is a clean tree rooted at `openai/router.py → openai/chat.py`. All cross-package edges go from `openai/ → proxy/` or to `config/`, `graph/`, `trace/`, `filter_adapter/`. The `extraction.py → proxy/locks` deferred import prevents a theoretical cycle (no actual back-edge exists even without deferral, but deferral is a correct defensive measure).

---

## Known Concerns Verification

### K1: openai/ circular imports
**Status: RESOLVED**
Import DAG is fully acyclic. `extraction.py` defers `proxy/locks` import as a defensive measure. No back-edges from `helpers.py` to any sibling. `chat.py` imports all siblings but nothing imports `chat.py` back (except `router.py` which is the mount point).

### K2: Streaming synthetic tool bug — `_wrap_response_as_sse()` tool_calls delta drop
**Status: RESOLVED**
`_non_streaming_to_sse()` (proxy/streaming.py:327-428) correctly emits tool_calls deltas in OpenAI streaming format: name delta with `{index, id, type, function: {name, arguments: ""}}`, followed by arguments delta `{index, function: {arguments: "..."}}`, followed by final delta with `finish_reason`. `yield_as_sse()` and `_wrap_response_as_sse()` both use `_non_streaming_to_sse()`. The recall re-send path in `openai/streaming.py:378` uses `yield_as_sse(second_data)` which correctly converts the non-streaming response to SSE with tool_calls deltas preserved.

**New risk (F3):** The streaming recall detection itself has a blind spot for secondary recall tool calls — see F3 above.

### K3: Curator prefix cache — md5 fingerprint + boundary detection
**Status: IMPROVED (partial)**
- `_fingerprint_message()` uses `hashlib.md5(raw.encode()).hexdigest()` — full 128-bit hash, not the `[:8]` prefix that the prior audit flagged. The `[:8]` truncation was from an older version. Current code uses the **full md5 hex** (32 chars / 128 bits) for boundary comparison, making collision probability negligible for fingerprint use.
- `_curator_caches` now has `_MAX_SESSIONS=200` cap with FIFO eviction — unbounded growth is **RESOLVED**.
- Residual concern (F4): md5 is still used instead of sha256 (inconsistent with session.py).

---

## Metrics

| File | LOC | Imports (in-chunk) | Imports (out-chunk) | Classes | Functions |
|------|-----|--------------------|---------------------|---------|-----------|
| proxy/streaming.py | 667 | 0 | 5 | 3 | 6 |
| proxy/rewrite.py | 691 | 1 | 1 | 0 | 13 |
| proxy/synthetic_tools.py | 551 | 0 | 3 (deferred) | 2 | 8 |
| proxy/agent_solo.py | 311 | 0 | 1 (deferred) | 1 | 7 |
| proxy/session.py | 350 | 0 | 2 | 0 | 8 |
| proxy/circuit_breaker.py | 213 | 0 | 1 (deferred) | 1 | 7 |
| proxy/tool_injection.py | ~150 | 0 | 1 (deferred) | 0 | 5 |
| proxy/recall.py | ~200 | 1 | 2 (deferred) | 0 | 4 |
| proxy/tool_intercept.py | ~180 | 0 | 1 (deferred) | 0 | 3 |
| proxy/upstream.py | ~120 | 0 | 2 | 0 | 2 |
| proxy/live.py | ~100 | 0 | 0 | 0 | 3 |
| proxy/locks.py | 91 | 0 | 0 | 0 | 4 |
| openai/chat.py | ~400 | 4 | 8 | 0 | 3 |
| openai/streaming.py | 476 | 2 | 6 | 0 | 1 |
| openai/non_streaming.py | ~250 | 0 | 5 | 0 | 1 |
| openai/extraction.py | ~200 | 1 | 4 | 0 | 2 |
| openai/file_cache.py | ~150 | 1 | 2 | 0 | 3 |
| openai/helpers.py | ~180 | 0 | 3 | 0 | 6 |
| openai/passthrough.py | 58 | 0 | 1 | 0 | 1 |
| openai/models.py | ~60 | 0 | 2 | 0 | 1 |
| openai/router.py | ~30 | 3 | 0 | 0 | 0 |
| openai/errors.py | ~40 | 0 | 0 | 2 | 0 |
| openai/schemas.py | ~50 | 0 | 0 | 0 | 0 |

---

## Recommendations (ordered by impact)

1. **F1 — DSML over-stripping (High):** Change `_DSML_BLOCK_RE` to use a bounded end-marker similar to `_DEEPSEEK_TOOL_BLOCK_RE`, e.g. `<｜｜DSML｜｜.*?<｜｜end｜｜>` or limit to the first N characters after the marker. If no end-marker convention exists, cap the match to `.{0,5000}` instead of `.*` to prevent eating the entire rest of the string on false positives.

2. **F2 — content-encoding passthrough (High):** Remove `content-encoding` from `HOP_BY_HOP_HEADERS` in `passthrough.py`. The proxy reads the full body via `resp.aread()`, so httpx has already decompressed the content — the correct behavior is to strip `content-encoding` from the **response** (since the body is now decompressed) but NOT from the **request** (let the client decide). Alternatively, add `accept-encoding: identity` to request headers to prevent upstream compression, then the current header stripping is safe.

3. **F3 — Secondary recall tool call in streaming (High):** After the streaming passthrough decision is made (non-recall tool detected first), continue monitoring `accumulator.tool_calls` for a secondary `__archolith_recall` call. If detected, buffer the remaining stream and handle the recall after the initial tool calls complete. This aligns with the non-streaming path behavior. Alternatively, document this as a known limitation and ensure the non-streaming path is always used when recall is injected (the current code already re-sends as non-streaming after recall interception, so the gap is only during the initial streaming detection phase).

4. **F4 — md5 fingerprint (Medium):** Replace `hashlib.md5()` in `agent_solo.py:96` with `hashlib.sha256()` for consistency with `session.py:163`. The first 16 hex chars of sha256 provide the same fingerprint quality without the deprecated-algorithm signal.

5. **F5 — Unbounded session locks (Medium):** Add automatic eviction to `_session_locks` similar to `circuit_breaker.py`'s OrderedDict LRU. Call `cleanup_stale_locks()` from `get_session_lock()` when the dict exceeds the threshold, or integrate with the existing `prune_session_state` cycle.

6. **F6 — Benchmark session globals (Medium):** Replace module-level globals with an `asyncio.Lock`-protected context object, or at minimum add a comment documenting the single-event-loop assumption so future deployers know not to use multi-threaded workers.

7. **F7 — threading.RLock in async context (Medium):** Replace `threading.RLock` with `asyncio.Lock` in `circuit_breaker.py`. Since all callers are in async context and mutations are quick dict ops, the conversion is straightforward and eliminates the theoretical event-loop stall risk.

8. **F8 — Silent synthetic tool strip (Medium):** In `_fallback_strip_synthetic`, preserve any text content that appeared before the tool calls in the model's message, rather than replacing the entire content with the hardcoded fallback message only when content is empty.

9. **F10 — Duplicate user-first logic (Low):** Remove the inline filter at `rewrite.py:274-278` and rely solely on `_ensure_user_first()` at line 680, which is cleaner and handles edge cases correctly.

10. **F11 — Curator cache per-entry size (Low):** Add a max-size check on the `rewritten` list stored in `_CuratorCache`. If the rewritten messages exceed a token/char budget, truncate or skip caching for that session to bound per-entry memory.
