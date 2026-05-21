# Resilience & Chaos Audit — <Title/Session>

**Date:** YYYY-MM-DD  
**Auditor:** <Who ran the audit>  
**Commit:** <HEAD at time of test>  
**Branch:** <Branch name>  

---

## 1. Downstream Outage Simulations

Simulate complete failure of downstream services and document proxy behavior. Verify that the proxy degrades gracefully without crashing the client request.

| Dependency | Simulation Method | Expected Behavior | Actual Behavior | Pass / Fail |
| :--- | :--- | :--- | :--- | :--- |
| **Graph Backend (Downtime)** | Neo4j: stop Docker container or block port 7687. LadybugDB: rename/delete `.lbug` file. | Proxy logs warning, degrades to `passthrough` / `cold_start` mode; client request succeeds. | | |
| **Graph Backend (Reconnect)** | Neo4j: restart container. LadybugDB: restore `.lbug` file and restart proxy. | Proxy recovers connection automatically on next turn; graph features resume. | | |
| **Extractor LLM (503)** | Mock / Inject 503 Service Unavailable | Client completes normally. Background extraction fails gracefully, logging the issue without blocking the user. | | |
| **Extractor LLM (429)** | Mock / Inject 429 Rate Limit | Extraction task retries with backoff or drops task gracefully; no impact to streaming response. | | |
| **Embedding API (Down)** | Mock / Inject timeout / error | Context assembly falls back to recency/priority-based relevance ranking; client request succeeds. | | |
| **Memory Engine (Down)** | Configure a memory adapter with an unreachable URL, or block the adapter's target endpoint | Proxy completes normally; fact promotion fails gracefully, logged to database / metrics. Requires `PROMOTION_ENABLED=true`. | | |

---

## 2. High Latency & Timeout Tolerances

Validate latency handling. The proxy must not hang indefinitely if a dependency is unresponsive.

- [ ] **Upstream LLM Timeout:** Simulate a 30s delay in upstream response. Verify proxy handles connection timeout, logs the incident, and returns an appropriate error or retries.
- [ ] **Graph Query Timeout:** Neo4j: configure slow queries (5s+). LadybugDB: simulate slow disk I/O or lock contention. Verify the proxy cuts off the query and falls back to passthrough/cold-start within the configured limit, preventing high user TTFT.
- [ ] **Background Task Isolation:** Verify that slow fact extraction (e.g., taking 15s+) does not delay completion of subsequent requests in the same session.

---

## 3. Crash Recovery & Database State Integrity

Verify proxy and database stability under abrupt termination.

- [ ] **Hard Process Kill:** Abruptly terminate the proxy server process during active streaming and extraction.
  * **Result Check (Neo4j):** Verify that no orphaned database locks remain.
  * **Result Check (LadybugDB):** Verify that no corrupted `.lock` files remain. Note: hard kills are known to corrupt the WAL (`UNREACHABLE_CODE` assertion in `wal_record.cpp`). Delete both `.lbug` and `.lbug.wal` files before restart if corruption occurs.
- [ ] **LadybugDB WAL Recovery:** (If using LadybugDB) Hard kill the proxy process during active writes.
  * **Result Check:** Verify that LadybugDB recovers the write-ahead log (WAL) on restart without data corruption. If WAL is corrupted, document the failure — this is a known LadybugDB limitation.
- [ ] **Startup Recovery:** Start the proxy while the graph backend is offline.
  * **Result Check (Neo4j):** Confirm the proxy retries startup connection according to `NEO4J_MAX_RETRIES`, then successfully starts in passthrough mode when retries are exhausted.
  * **Result Check (LadybugDB):** Confirm the proxy logs an error and falls back to passthrough/cold-start mode if the `.lbug` file is missing or locked.

---

## Findings

### Issues Found
Severity: P1 (Critical blocker) / P2 (Degradation failure) / P3 (Minor warning)

1. **[P?]** <Issue Description> — <Evidence / Log snippet>

### Recommendations
1. <Tuning or code changes recommended to improve resilience>

---

## Conclusion
1-3 sentences: Does the proxy degrade gracefully under dependency failure? What is the primary resilience vulnerability?
