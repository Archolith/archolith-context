# Wrapup: archolith-context Concurrency Correctness

## Status: READY FOR REVIEW

| Field | Value |
|-------|-------|
| **Plan** | archolith-context-concurrency-correctness-plan.md |
| **Branch** | `main` (archolith-context repo) |
| **Commits** | `8807cc5` → `4d165d7` (4 commits) |
| **Date** | 2026-06-09 |
| **Author** | Claude Sonnet 4.5 |

---

## Summary

Fixed two concurrency defects identified in the 2026-06-09 audit. Tests written first (both confirmed failing before any fix), then each defect fixed and verified in isolation.

---

## Commits

| SHA | Message |
|-----|---------|
| `8807cc5` | test(extraction): add concurrency tests for lock-timeout and fingerprint-race |
| `a99e803` | fix(extraction): fail closed when session lock acquire times out |
| `58d1b3b` | fix(sessions): serialize find-or-create per fingerprint to prevent duplicate sessions |
| `4d165d7` | docs(archolith-context): update CHANGELOG with concurrency fixes |

---

## Files Changed

| File | Change |
|------|--------|
| `tests/test_extraction_concurrency.py` | Created — 2 new concurrency tests |
| `archolith_proxy/openai/extraction.py` | +7 lines — `return` after timeout warning |
| `archolith_proxy/graph/ladybug_sessions.py` | +28 -11 — module-level lock dict + double-checked create |
| `CHANGELOG.md` | +11 lines — unreleased section for both fixes |

---

## Definition of Done

- [x] `tests/test_extraction_concurrency.py` exists with both concurrency tests
- [x] Both concurrency tests **FAILED** before the fix (confirmed: Test 1 showed `extract_facts` called 1 time; Test 2 showed duplicate session IDs)
- [x] Both concurrency tests **PASS** after the fix
- [x] `tests/test_graph_session_pass1_fixes.py` still green — 4/4 passed, no modifications
- [x] Full pytest suite green — **896 passed, 0 failed** in 73.5s
- [x] `extraction.py`: TimeoutError branch contains `return` — no fall-through into write block
- [x] `ladybug_sessions.py`: `find_or_create_by_fingerprint` acquires per-fingerprint lock before create, with double-checked lookup inside the lock
- [x] No files outside the project touched
- [x] CHANGELOG updated

---

## Defect Details

### Defect #1 — extraction lock fails open (extraction.py:53-54)

**Before:** `except asyncio.TimeoutError:` caught the timeout and logged it but execution fell through into the full write block (file-cache upserts, fact dedup, graph writes) without holding the lock.

**Fix:** Added `return` as the last statement inside the `except asyncio.TimeoutError:` block, after the existing `logger.warning` call. Also expanded the log message with `note="skipping extraction — fail closed"`.

**Test:** `test_extraction_skips_when_lock_timeout` — patches `asyncio.wait_for` to raise `TimeoutError`, patches `get_session_lock`, and asserts `extract_facts` is NOT called. Before fix: called 1 time. After fix: not called.

### Defect #2 — find_or_create_by_fingerprint race (ladybug_sessions.py:98-116)

**Before:** Two concurrent first requests both called `find_session_by_fingerprint`, both got `None`, then both called `create_session` — producing two Session nodes with the same fingerprint.

**Fix:** Added `_fingerprint_create_locks: dict[str, asyncio.Lock] = {}` at module level. The slow path (no existing session) acquires a per-fingerprint lock, then does a double-checked lookup inside the lock before creating. The fast path (session already exists) skips the lock entirely.

**Test:** `test_find_or_create_concurrent_same_fingerprint` — runs `asyncio.gather` of two concurrent `find_or_create_by_fingerprint` calls against a real in-memory LadybugDB. Asserts both results have the same `session_id` and that exactly 1 Session node exists for the fingerprint. Before fix: two different session IDs, count=2. After fix: same ID, count=1.

---

## Verification Evidence

```
tests/test_extraction_concurrency.py::test_extraction_skips_when_lock_timeout PASSED
tests/test_extraction_concurrency.py::test_find_or_create_concurrent_same_fingerprint PASSED
tests/test_graph_session_pass1_fixes.py::test_find_by_fingerprint_returns_session_when_present PASSED
tests/test_graph_session_pass1_fixes.py::test_find_by_fingerprint_returns_none_when_absent PASSED
tests/test_graph_session_pass1_fixes.py::test_find_or_create_by_fingerprint_atomic_creates_once PASSED
tests/test_graph_session_pass1_fixes.py::test_find_or_create_by_fingerprint_different_fingerprints PASSED

Full suite: 896 passed, 242 warnings in 73.50s
```

---

## Deferred

- Defects #3–#5 (dedup capacity, hash width, WAL error message) → Plan 2 (archolith-context-dedup-hardening)
- `_fingerprint_create_locks` growth is unbounded; acceptable for single-process deployment with bounded client count — can add eviction in a follow-up if client churn is high
- Per-fingerprint lock only serializes in-process; if the proxy runs multi-process (multiple uvicorn workers), cross-process races are still possible — LadybugDB is currently single-process, assumption documented in the function docstring

---

## Gaps

None. All plan items completed. No existing tests modified.
