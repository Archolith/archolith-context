# Concurrency & Resource Leak Audit — <Title/Session>

**Date:** YYYY-MM-DD  
**Auditor:** <Who ran the audit>  
**Commit:** <HEAD at time of test>  
**Branch:** <Branch name>  

---

## 1. Memory Leak Profile

Monitor the proxy process RSS memory over a long-running test simulation (e.g., 100+ simulated sessions running concurrently or sequentially over 10 turns).

| Stage | RSS Memory (MB) | Active Sessions | Notes |
| :--- | :--- | :--- | :--- |
| **Startup / Initial** | | 0 | Baseline |
| **Mid-Test (Peak Load)** | | | |
| **Post-Test (Idle)** | | 0 (All sessions completed) | |
| **Post-Garbage Collection** | | 0 | Run manual GC `gc.collect()` |

### Cache & Collection Growth Checks:
- [ ] **TraceStore Bound Check:** Verify that `TraceStore` elements (`self._by_session`, `self._by_turn_id`) are capped and evict old sessions.
- [ ] **Lock Registry Eviction:** Verify that locks in `_session_locks` are removed via `cleanup_session_lock()` when a session expires or ends.
- [ ] **Embedding Cache eviction:** Verify that the query `_embedding_cache` does not grow indefinitely (e.g., uses bounded LRU cache or TTL cache).
- [ ] **WS connections cleanup:** Verify that WebSocket connections for `/ws/stream` are correctly cleaned up on client disconnect.

---

## 2. Concurrency & Locking Integrity

- [ ] **Stale-Read Prevention (Turn-locking):** Send Turn N+1 to the proxy *before* the background extraction task for Turn N has finished.
  * **Result Check:** Confirm the proxy blocks Turn N+1 assembly until Turn N extraction writes are committed or timeout occurs, preventing the model from receiving stale context.
- [ ] **Deadlock Verification:** Run 5 concurrent clients hitting the same session ID. Verify all requests complete without deadlocks.
- [ ] **TTL / Clean-up Execution:** Run `cleanup.py` tasks (`expire_sessions` and `delete_expired_sessions`). Verify that:
  1. Expired sessions are correctly marked and purged from the graph backend.
  2. Active sessions are completely untouched.

---

## 3. Database Connection Pooling

- [ ] **Graph Backend Connection Pool / Handle Leak:** Neo4j: Run 100 concurrent read/write queries; check active connection count and verify connections are returned to the pool. LadybugDB: Verify that `LADYBUG_MAX_CONCURRENT` semaphore limits are respected and not exhausted under load; confirm database handles are released after each query batch.
- [ ] **HTTP Client Reuse:** Verify that only a single instance of `httpx.AsyncClient` is created for upstream calls and reused across all requests, rather than initiating a new client per request.

---

## Findings

### Issues Found
Severity: P1 (System crash / high memory leak) / P2 (Degradation / slow leak) / P3 (Minor)

1. **[P?]** <Issue Description> — <Evidence / Growth metrics>

### Recommendations
1. <Remediation recommendations>

---

## Conclusion
1-3 sentences: Does the proxy maintain a flat memory footprint under sustained load? What is the main concurrency bottleneck?
