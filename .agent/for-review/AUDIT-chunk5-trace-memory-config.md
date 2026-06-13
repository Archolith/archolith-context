# Chunk 5 Audit: trace/, memory/, and Root Config/Entry Files

**Auditor:** OpenCode (nvidia/z-ai/glm-5.1)
**Date:** 2026-06-07
**Scope:** `archolith_proxy/trace/`, `archolith_proxy/memory/` (incl. all adapters), `config.py`, `main.py`, `metrics.py`, `logging_config.py`, `admin.py`, `filter_adapter.py`
**Prior Audit Status:** No prior chunk-5 audit found in `.agent/for-review/`.

---

## Import DAG

```
config.py ──────────────────────────────────────────────┐
metrics.py (standalone)                                  │
logging_config.py (standalone)                           │
admin.py ───> config.py                                  │
filter_adapter.py (standalone)                           │
                                                         │
trace/__init__.py ───> trace.builder, trace.store        │
trace/builder.py ───> models.dtos                        │
trace/store.py ───> models.dtos, config.py (lazy), graph.backend (lazy)
trace/router.py ───> graph.backend, proxy.session, trace.store, config.py (lazy), extractor.client (lazy), extractor.dedup (lazy)
                                                         │
memory/__init__.py ───> memory.models, memory.registry   │
memory/models.py (standalone)                            │
memory/registry.py ───> memory.adapters.base (TYPE_CHECKING), memory.models (TYPE_CHECKING)
memory/promotion.py ───> memory.adapters.base, memory.models, memory.registry
memory/adapters/__init__.py ───> memory.adapters.base    │
memory/adapters/base.py ───> memory.models               │
memory/adapters/* ───> memory.adapters.base, memory.models, shared.text_utils
                                                         │
main.py ───> admin, config, graph.backend, graph.neo4j_backend,│
             logging_config, metrics, openai.router,     │
             routers.*, trace.router, trace.store,       │
             memory.registry, memory.promotion,          │
             curator.tools, proxy.live, filter_adapter    │
```

**Cycles detected:** None. All cross-module imports are either top-level within the same subtree or lazy (`trace/store.py:437`, `trace/store.py:477`, `trace/router.py:330-396`) to avoid cycles. The `TYPE_CHECKING` guard in `memory/registry.py:15` correctly breaks the only potential cycle (registry ↔ adapters.base ↔ models).

---

## Findings

### C-01: Admin endpoints open by default when ADMIN_TOKEN is empty
**Severity: High**
**File:** `admin.py:22-23`
**Category: Security**

```python
if not settings.admin_token:
    return  # No token configured — open access (localhost assumption)
```

The admin dependency skips all auth when `ADMIN_TOKEN` is empty. The comment says "localhost assumption" but there is no actual localhost check — if the proxy is exposed on a network interface, all admin/trace/memory/session endpoints are unauthenticated. The `require_admin_token` dependency protects: `admin_router`, `sessions_router`, `memory_admin_router`, `trace_router` (`main.py:360-364`).

**Risk:** An attacker on the same network can read all session traces (including original messages with potentially sensitive user data), manipulate benchmark session IDs, invoke the QA extraction endpoint, and modify runtime config via `PATCH /admin/config`.

### C-02: Upstream API key sent in readiness/health probes
**Severity: High**
**File:** `main.py:273-277`, `main.py:413-417`, `main.py:460-465`
**Category: Security**

```python
resp = await app.state.http_client.get(
    f"{settings.upstream_api_url}/models",
    headers={"Authorization": f"Bearer {settings.upstream_api_key}"},
    timeout=5.0,
)
```

The upstream API key is sent in a bearer header on every `/ready` and `/health` call. These are unauthenticated endpoints (no `require_admin_token` dependency). An external monitor hitting `/health` every 10s creates a steady stream of API-key-bearing requests. If the upstream is a shared service, the key is visible in upstream access logs triggered by unauthenticated callers.

**Mitigation:** Cache the connectivity check result; don't re-probe on every health call. Or use a separate health-check credential.

### C-03: Secret values leaked via `get_settings_delta()` and `snapshot_config()` exclusion gaps
**Severity: Medium**
**File:** `config.py:351-361`, `config.py:366-378`
**Category: Security**

`get_settings_delta()` returns `base` and `current` dicts containing **all** fields, including secrets (`upstream_api_key`, `session_neo4j_password`, etc.). The `_SNAPSHOT_EXCLUDE` set only protects `snapshot_config()`, not `get_settings_delta()`. Any code or endpoint that calls `get_settings_delta()` and serializes the result leaks secrets.

Additionally, `memory_api_key` is in `_SNAPSHOT_EXCLUDE` but `memory_api_url` is also excluded — while `prepper_api_key` and `assembler_api_key` are excluded, but `upstream_api_key` is excluded yet still present in `get_settings_delta()`.

### C-04: Trace disk writes outside the asyncio lock
**Severity: Medium**
**File:** `trace/store.py:141-151`
**Category: Correctness**

The comment says "I/O should not block reads" but the file append at line 148 occurs **after** the `async with self._lock` block exits. In a concurrent scenario, two tasks could interleave writes to the same JSONL file. While OS-level append is typically atomic for small writes, Python's buffered I/O + async means the actual `f.write()` call can interleave if two tasks record traces for the same session concurrently. This can produce malformed JSONL (two partial lines interleaved).

### C-05: `_extraction_in_flight` global flag — race window on flag clear
**Severity: Medium**
**File:** `trace/router.py:300-307`, `trace/router.py:436-438`
**Category: Correctness**

The extraction rate-limit uses a module-level `_extraction_in_flight` bool guarded by `_extraction_lock`. However, the clear at line 437 re-acquires the lock inside a `finally` block, which is correct. But the **set** at line 307 and the check at line 302 are within the same `async with _extraction_lock`, meaning only one extraction can run at a time. This is more of a mutex than a rate limit. If the extraction call at line 339 takes 60s (Cognee timeout), all other QA extract callers get 429 for that entire duration. This is intentional but undocumented as a global bottleneck.

### C-06: `config.py` dual-mode defaults vs README bootstrap path
**Severity: Medium**
**File:** `config.py:48-49`, `config.py:62-65`, `config.py:124-125`
**Category: Maintainability / Confusion**

As documented in the known concern, defaults are:
- `upstream_base_url = "https://api.deepseek.com/v1"` (line 48)
- `graph_backend = "neo4j"` (line 124)

But the README and `.env.example` are optimized for LadybugDB + OpenAI. This means a developer following the README gets one mental model, while reading the code gives another. The Settings class comment at line 1-12 does not explain this duality.

**Status:** Known and documented in AGENTS.md. The dual-mode is acknowledged as valid. No code bug, but a developer-experience hazard.

### C-07: Session ephemerality — 24h TTL, in-memory state, data loss on restart
**Severity: Medium**
**File:** `config.py:73`, `trace/store.py:8-11`, `memory/promotion.py:72`
**Category: Performance / Data Loss**

- `session_ttl_hours: int = 24` (config.py:73) — sessions expire after 24h.
- TraceStore is in-memory with optional disk persistence (trace_dir). Without `trace_dir`, all traces are lost on restart.
- `PromotionService._audit` (promotion.py:72) is a plain `list[PromotionResult]` — unbounded growth, no eviction, lost on restart.
- `_metrics` dict (metrics.py:13) is in-memory only.
- `TraceStore._session_meta` (store.py:65) repopulates on next request after LRU eviction (good), but is lost on process restart.

**Mitigation exists:** `trace_dir` enables JSONL persistence; `trace_retention_days` enables cleanup. But promotion audit and metrics have no persistence.

### C-08: turn_number SSOT — dual storage in trace and graph
**Severity: Medium**
**File:** `trace/store.py:408-414`, `graph/session.py:108`, `models/dtos.py:64`
**Category: Correctness / SSOT**

`turn_number` is stored both in:
1. `TurnTrace.turn_number` (dtos.py:64) — set by TraceBuilder during request handling
2. `Session.turn_number` in the graph (session.py:108) — incremented via Cypher `SET s.turn_number = s.turn_number + 1`

These can diverge. The consistency check at `trace/store.py:429-461` detects orphans/mismatches but only at startup. During runtime, if the graph increment fails (Neo4j transient error), `graph_turn` lags behind the trace-stored `turn_number`. The trace builder gets turn_number from a different code path than the graph increment — they are not atomic.

**Status:** Partial mitigation via `verify_consistency()`. The mismatch tolerance at line 451 (`max_trace_turn > graph_turn + 1`) allows a drift of 1, which masks single-increment failures.

### C-09: RTK/filter fail-open sentinel pattern correctness
**Severity: Low**
**File:** `filter_adapter.py:16-45`, `filter_adapter.py:77-83`
**Category: Correctness**

The 3-state sentinel (`_UNRESOLVED` / callable / `None`) is correctly implemented:
- `_LoadState.UNRESOLVED` → never loaded yet
- `callable` → package available
- `None` → package absent (fail-open)

The `is_available()` function (line 77) correctly checks for callable. The fail-open path in `filter_tool_messages` (line 92) and `filter_single_tool_result` (line 129) returns input unchanged when filter is `None`.

**Issue:** The sentinel is module-level mutable state. In a multi-threaded environment, two threads could race on `_load_filter_output()`. The GIL protects the assignment but not the import side-effect. This is a theoretical concern only — Python's import system is thread-safe.

**Additionally:** `main.py:176-192` correctly fails fast at startup if `FILTER_ENABLED=true` but the package is missing, preventing the silent fail-open scenario in production.

### C-10: `_apply_overrides` — no type validation beyond casting
**Severity: Medium**
**File:** `config.py:339-348`
**Category: Security / Correctness**

```python
expected_type = type(getattr(settings, key))
setattr(settings, key, expected_type(value))
```

This allows any key in `config_overrides.json` that matches a Settings field to be force-set. A malicious override could:
- Set `admin_token` to an empty string (disabling auth) — line 238
- Set `filter_enabled` to `false` (disabling filtering)
- Set `session_ttl_hours` to `999999` (preventing session expiry)
- Set `upstream_base_url` to an attacker-controlled endpoint (API key exfiltration)

The `config_overrides.json` file is written by `PATCH /admin/config`, which is protected by `require_admin_token`. But if an attacker can write to the file on disk (e.g., via a directory traversal or file-write vuln elsewhere), they gain full config control.

### C-11: `PromotionService._audit` — unbounded in-memory growth
**Severity: Low**
**File:** `memory/promotion.py:72`, `memory/promotion.py:276`
**Category: Performance**

`self._audit: list[PromotionResult] = []` grows without bound. Every promotion appends to it. For long-running processes with active promotion, this list grows indefinitely. No eviction, no cap, no persistence. Contrast with `TraceStore` which has explicit caps (`max_turns_per_session`, `max_sessions`).

### C-12: `TraceStore._session_order` — O(n) LRU operations
**Severity: Low**
**File:** `trace/store.py:125-126`
**Category: Performance**

```python
self._session_order.remove(session_id)  # O(n)
self._session_order.append(session_id)
```

`list.remove()` is O(n) where n = number of sessions (up to `max_sessions=1000`). For each request, the session is moved to the end of the LRU list. With 1000 sessions and high QPS, this is a minor hotspot. An `OrderedDict` would give O(1) move-to-end.

### C-13: `_truncate_messages` deep-copies all messages on every request
**Severity: Low**
**File:** `trace/builder.py:280-300`
**Category: Performance**

Every `set_original_messages` or `set_rewritten_messages` call deep-copies and potentially truncates the entire message array. For large conversations (50K+ tokens), this copies significant data. The copy is necessary (to avoid mutating the original), but the truncation check happens per-field per-message even when nothing exceeds `_MAX_MESSAGE_CONTENT=2000`.

### C-14: Adapter boilerplate duplication — 9 adapters with near-identical structure
**Severity: Low**
**File:** `memory/adapters/*.py`
**Category: Maintainability / AI Anti-pattern**

All 9 adapter files follow the same pattern: `__init__`, `_get_client`, `validate_config`, `capabilities`, `close`, `healthcheck`, `promote_fact`, `_build_payload`. The only meaningful differences are: endpoint paths, auth header format, and payload shape. This is a typical AI code-generation artifact — each adapter was likely scaffolded from the same template rather than factoring common HTTP-client lifecycle into a base class method.

**Recommendation:** Add an `HttpMemoryAdapterBase(MemoryAdapterBase)` that provides `_get_client`, `close`, `healthcheck`, and the try/except/result pattern. Each concrete adapter overrides only `_build_payload`, endpoint paths, and auth format.

### C-15: `nocturne_memory.py:239` — `min(10, int(promotion.confidence * 10))` crashes on `None`
**Severity: Medium**
**File:** `memory/adapters/nocturne_memory.py:239`
**Category: Correctness**

```python
"priority": min(10, int(promotion.confidence * 10)),
```

`PromotionRecord.confidence` is `float | None` (models.py:49). If confidence is `None`, `None * 10` raises `TypeError`. The other adapters that include confidence in metadata dicts handle this fine (JSON serialization of `None` → `null`), but this line does arithmetic on it.

### C-16: `generic_http.py:154` — `str.format()` with user-controlled content is a format-string injection risk
**Severity: Medium**
**File:** `memory/adapters/generic_http.py:154`
**Category: Security**

```python
payload[key] = value.format(
    content=promotion.content, fact_type=promotion.fact_type, ...
)
```

If `value` (from `config.extra["payload_template"]`) contains `{__class__}` or similar Python format-string tricks, it could leak internal state. More practically, user-controlled `promotion.content` containing `{0}` or `{1}` would raise `IndexError` if `value` has positional placeholders. The risk is limited because the template comes from operator config, not user input, but promotion.content IS derived from user messages.

### C-17: `list_sessions()` holds lock while computing summaries for ALL sessions
**Severity: Low**
**File:** `trace/store.py:298-340`
**Category: Performance**

`list_sessions()` computes full summaries for every session while holding `self._lock`. For 1000 sessions each with 100 turns, this blocks all trace recording/writes for the duration of the computation. `get_session_summary()` also holds the lock but for one session.

### C-18: `trace/router.py:141-179` — no auth on graph explorer GET endpoints
**Severity: Low**
**File:** `trace/router.py:32`
**Category: Security**

The trace router has `dependencies=[Depends(require_admin_token)]` applied in `main.py:364`. This is correct — all trace endpoints require admin auth when `ADMIN_TOKEN` is set. No bypass found. (Confirmed: line 364 applies to the entire router.)

---

## Known Concerns Summary

| Concern | Status | Finding |
|---------|--------|---------|
| Config dual-mode (Neo4j+DeepSeek vs LadybugDB+OpenAI) | **Confirmed** | C-06. No bug, developer-experience hazard. Documented in AGENTS.md. |
| Session ephemerality (24h TTL, in-memory, restart loss) | **Confirmed** | C-07. Trace disk persistence is opt-in. Promotion audit and metrics have no persistence. |
| Trace SSOT (turn_number in trace + graph) | **Confirmed** | C-08. Dual storage can diverge. `verify_consistency()` detects at startup; allows drift of 1. |
| RTK fail-open sentinel pattern | **Correct** | C-09. 3-state sentinel is properly implemented. Startup fails fast on `FILTER_ENABLED=true` + missing package. |

---

## Findings by Severity

| ID | Severity | Category | File:Line | Summary |
|----|----------|----------|-----------|---------|
| C-01 | **High** | Security | `admin.py:22-23` | Admin endpoints open when ADMIN_TOKEN empty (no localhost check) |
| C-02 | **High** | Security | `main.py:273-277` | Upstream API key sent in bearer header on unauthenticated /health probes |
| C-03 | **Medium** | Security | `config.py:351-361` | `get_settings_delta()` leaks all secrets; exclusion only covers `snapshot_config()` |
| C-04 | **Medium** | Correctness | `trace/store.py:141-151` | JSONL disk writes outside asyncio lock — interleaving risk |
| C-05 | **Medium** | Correctness | `trace/router.py:300-307` | QA extract global mutex, not just rate limit — 60s blocks all callers |
| C-06 | **Medium** | Maintainability | `config.py:48-49,124` | Dual-mode defaults vs README confusion |
| C-07 | **Medium** | Data Loss | `config.py:73`, `promotion.py:72` | Ephemeral state: promotion audit unbounded + lost on restart |
| C-08 | **Medium** | SSOT | `trace/store.py:408-414` | turn_number dual storage can diverge at runtime |
| C-10 | **Medium** | Security | `config.py:339-348` | Config overrides allow any field mutation (incl. admin_token, upstream URL) |
| C-15 | **Medium** | Correctness | `nocturne_memory.py:239` | `confidence * 10` crashes when confidence is None |
| C-16 | **Medium** | Security | `generic_http.py:154` | `str.format()` with promotion content — format-string injection risk |
| C-11 | **Low** | Performance | `promotion.py:72,276` | Unbounded _audit list growth |
| C-12 | **Low** | Performance | `trace/store.py:125-126` | O(n) LRU list.remove on every request |
| C-13 | **Low** | Performance | `trace/builder.py:280-300` | Deep-copy + truncation of all messages every request |
| C-14 | **Low** | AI Anti-pattern | `memory/adapters/*.py` | 9 near-identical adapter files — should factor HTTP base class |
| C-17 | **Low** | Performance | `trace/store.py:298-340` | list_sessions holds lock while computing all summaries |
| C-09 | **Low** | Correctness | `filter_adapter.py:16-45` | Sentinel pattern correct; theoretical thread-race on lazy import |
