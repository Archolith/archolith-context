# AUDIT: Chunk 3 — archolith_context/graph/ Subsystem

**Date**: 2026-06-07
**Auditor**: GLM-5.1 (harness session `archolith-audit-chunk3`)
**Scope**: `archolith_proxy/graph/` directory (17 files, ~2,300 LOC)
**Prior Audits**: 3 adversarial reviews (context quality remediation, RTK quality remediation, two-pass vs two-curator)
**Architecture Ref**: `archolith-context/.agent/architecture.md`

---

## Files Audited

| File | Role |
|------|------|
| `graph/__init__.py` | Package init |
| `graph/neo4j_backend.py` | Neo4j graph backend (stubs for unsupported methods) |
| `graph/ladybug_backend.py` | LadybugDB graph backend |
| `graph/ladybug_sessions.py` | Session management (LadybugDB) |
| `graph/ladybug_edges.py` | Edge CRUD (LadybugDB) |
| `graph/ladybug_facts.py` | Fact storage (LadybugDB) |
| `graph/ladybug_checkpoint.py` | Checkpoint management (LadybugDB) |
| `graph/cleanup.py` | TTL cleanup (standalone Cypher) |
| `graph/facts.py` | Fact model + Cypher WHERE clause construction |
| `graph/sessions.py` | Session model |
| `graph/edges.py` | Edge model |
| `graph/decisions.py` | Decision queries |
| `graph/files.py` | File outline management |
| `graph/edge.py` | Edge data model |

---

## Findings

### HIGH

#### H1 (F-01): 21 NotImplementedError raises in Neo4j backend — 9+ unguarded callers crash 500
**File**: `neo4j_backend.py` (throughout)
**Category**: Correctness / Security
**Detail**: The Neo4j backend raises `NotImplementedError` for 21 methods that are LadybugDB-only. At least 9 callers in the proxy pipeline do not catch this exception, meaning any request hitting an unsupported Neo4j method returns an HTTP 500 instead of a graceful degradation or 501 Not Implemented. This is a crash-level defect when Neo4j is selected as the backend.
**Prior Status**: CONFIRMED — Known concern #1 from task spec. Still exists.
**Recommendation**: Replace `raise NotImplementedError` with fail-open stubs that return empty results and log a warning. Alternatively, add a `supported_methods()` check at startup to refuse Neo4j as backend when file cache or other LadybugDB-only features are required.

#### H2 (F-02): turn_number dual-store SSOT violation with O(N) reconciliation
**File**: `sessions.py`, `ladybug_sessions.py`, `neo4j_backend.py`
**Category**: Correctness / Performance
**Detail**: `turn_number` is stored in both the trace store and the graph store. When they diverge (e.g., after a partial write), reconciliation requires an O(N) scan of all edges in the session to find the maximum turn number. This is a SSOT violation — there is no `set_turn_number()` for atomic reconciliation, so the value can only be derived by scanning.
**Prior Status**: CONFIRMED — Known concern #2 from task spec.
**Recommendation**: Add `set_turn_number()` for atomic updates. Make the trace store authoritative and derive graph turn_number from it rather than maintaining a separate copy.

---

### MEDIUM

#### M1 (F-03): Fake-batch loops instead of UNWIND in 6 LadybugDB bulk functions
**File**: `ladybug_edges.py:15-110`, `ladybug_checkpoint.py:69-167`
**Category**: Performance
**Detail**: 6 "bulk" functions (issues, verifications, decisions, touches, and others) loop over individual INSERT statements instead of using Cypher UNWIND for batch operations. `store_facts_batch` correctly uses UNWIND, but the others do not.
**Prior Status**: PARTIAL — `store_facts_batch` is correct; 6 others are not.
**Recommendation**: Convert all 6 loop-based bulk functions to UNWIND-based batch Cypher.

#### M2 (F-04): No explicit transactions in LadybugDB — multi-step operations not atomic
**File**: `ladybug_backend.py:338`, `ladybug_sessions.py:41-59`, `ladybug_edges.py:32-68`
**Category**: Atomicity
**Detail**: Multi-step operations (e.g., lookup + create in `find_or_create_by_fingerprint`) execute as separate queries without transaction wrapping. Under asyncio, coroutines yield at `await` points, so interleaving IS possible when two requests with the same fingerprint arrive concurrently (TOCTOU race).
**Prior Status**: CONFIRMED — Known concern #4.
**Recommendation**: Add explicit transaction support to `_execute()` or a `_execute_transaction()` context manager. Wrap multi-step operations in transactions.

#### M3 (F-05): File cache silently disabled on Neo4j — no operator warning
**File**: `neo4j_backend.py:246-269`, `proxy/tool_intercept.py:117-150`, `openai/file_cache.py:31-77`
**Category**: Correctness / Feature-parity
**Detail**: The file cache (`upsert_file_content`, `get_file_content`, etc.) is LadybugDB-only. When Neo4j is the backend:
- `tool_intercept.py:130` calls `backend.get_file_content()` → `NotImplementedError` → caught by try/except but returns `None`, silently skipping all file cache hits
- `file_cache.py:49` calls `backend.upsert_file_content()` → `NotImplementedError` → caught but file is never cached
- `file_cache_enabled` config setting at `extraction.py:70` is not gated on backend type

The entire file cache pipeline is silently no-op'd when using Neo4j, with no indication to the operator.
**Prior Status**: CONFIRMED — Known concern #5.
**Recommendation**: Either (a) add a startup warning when `file_cache_enabled=True` and backend is Neo4j, (b) implement a Neo4j-backed file cache, or (c) gate `file_cache_enabled` automatically based on `supported_methods()`.

#### M4 (F-06): Cypher injection risk — dynamic WHERE clause via f-string
**File**: `facts.py:256-259`, `ladybug_facts.py:145-148`
**Category**: Security
**Detail**: The WHERE clause in fact queries is constructed via f-string interpolation. Currently safe because the values come from controlled enum/constants, but fragile — a future refactor that passes user input could introduce injection.
**Prior Status**: NEW
**Recommendation**: Use parameterized Cypher queries (`$param` syntax) instead of f-string interpolation for WHERE clauses.

#### M5 (F-07): Dual implementations of TTL cleanup in different Cypher dialects
**File**: `cleanup.py:1-45` vs `ladybug_backend.py:523-552`
**Category**: Maintainability
**Detail**: TTL cleanup is implemented twice — once in `cleanup.py` (standalone Cypher) and once in `ladybug_backend.py` (as a method). The two implementations use different Cypher dialects and may drift in behavior.
**Recommendation**: Consolidate into a single implementation. The backend method should be the canonical one; `cleanup.py` should delegate to it.

#### M6 (F-09): Dead code — `_rotation_depth = 0` before `raise` never executes
**File**: `ladybug_backend.py:239-244`
**Category**: Correctness
**Detail**: An assignment `_rotation_depth = 0` immediately before a `raise NotImplementedError` is dead code — the variable is never read after the exception is raised.
**Recommendation**: Remove the dead assignment.

#### M7 (F-10): TOCTOU race on fingerprint lookup+create
**File**: `ladybug_sessions.py:41-59`
**Category**: Concurrency
**Detail**: `find_or_create_by_fingerprint` does a lookup, then an insert if not found. Between the lookup `await` and the insert, another coroutine could insert the same fingerprint. This is a specific instance of M2 (no transactions).
**Recommendation**: Wrap in a transaction or use MERGE with ON CREATE for atomic upsert.

#### M8 (F-11): `get_file_outline` fetches all outlines into memory for in-process filter
**File**: `files.py:184-188`
**Category**: Performance
**Detail**: `get_file_outline` fetches all file outlines into Python memory and filters in-process rather than pushing the filter to the database query. This is an N+1-style concern that loads potentially large result sets unnecessarily.
**Recommendation**: Push the filter predicate into the Cypher query using a WHERE clause.

---

### LOW

#### L1 (F-08): Import-time protocol check is nearly meaningless
**File**: `neo4j_backend.py:337-346`
**Category**: Maintainability
**Detail**: The import-time check for protocol compliance only verifies method names, not signatures or return types. It provides a false sense of safety.
**Recommendation**: Use `typing.Protocol` with `@runtime_checkable` for stronger verification, or remove the check entirely.

#### L2 (F-12): Single-column dict unwrapping in `_execute` is confusing/fragile
**File**: `ladybug_backend.py:355-363`
**Category**: Correctness
**Detail**: When a Cypher query returns a single column, `_execute` automatically unwraps the dict into a flat list. This is fragile — if a query adds a second column, the unwrapping silently breaks.
**Recommendation**: Make unwrapping opt-in via a parameter, not automatic.

#### L3 (F-13): Empty WHERE clause when `include_superseded=True`
**File**: `decisions.py:64-67`
**Category**: Cosmetics
**Detail**: When `include_superseded=True`, the generated Cypher has an empty WHERE clause (`WHERE `) which is valid but sloppy.
**Recommendation**: Omit the WHERE keyword entirely when there are no conditions.

#### L4 (F-14): Inconsistent status parameter type (FileStatus enum vs str)
**File**: `edge.py:25` vs `ladybug_edges.py:32`
**Category**: Type safety
**Detail**: `edge.py` uses `FileStatus` enum for the status field, but `ladybug_edges.py` accepts `str`. This inconsistency could allow invalid status values through the LadybugDB path.
**Recommendation**: Use `FileStatus` enum consistently in both paths.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High | 2 |
| Medium | 8 |
| Low | 4 |
| **Total** | **14** |

### Prior Audit Concerns Checklist

| # | Known Concern | Status | Finding |
|---|---------------|--------|---------|
| 1 | Neo4j stub hardening — NotImplementedError causes 500 crashes | CONFIRMED | F-01: 21 raises, 9+ unguarded callers |
| 2 | trace/graph SSOT — turn_number in both stores | CONFIRMED | F-02: Divergence possible; O(N) reconciliation loop |
| 3 | LadybugDB UNWIND support | PARTIAL | F-03: `store_facts_batch` uses UNWIND; 6 others use loops |
| 4 | Transaction boundary leakage | CONFIRMED | F-04: No explicit transactions; multi-step ops not atomic |
| 5 | File cache LadybugDB-only | CONFIRMED | F-05: Silently no-op'd on Neo4j; no operator warning |

### Import DAG
No import cycles detected in the graph/ subsystem.

### AI Anti-Patterns
No AI-specific anti-patterns found. The code is deterministic with no LLM calls.
