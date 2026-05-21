# Memory Alignment & Sync Audit — <Title/Session>

**Date:** YYYY-MM-DD  
**Auditor:** <Who ran the audit>  
**Commit:** <HEAD at time of test>  
**Branch:** <Branch name>  

---

## 1. Fact Promotion Accuracy & Noise Gate

Analyze a sample of 20 promoted facts to evaluate whether they represent durable, generalizable knowledge or ephemeral, session-specific details.

- **Total facts in session graph:** ___
- **Total facts promoted to long-term memory:** ___
- **Durable (Generalizable):** ___
- **Session-specific / Ephemeral Noise:** ___

### Fact Classification Sampling:

| Promoted Fact String | Fact Type | Ephemeral Noise? (Yes/No) | Reason |
| :--- | :--- | :--- | :--- |
| *e.g., "The project uses Python 3.12"* | config | No | Valid generalizable fact |
| *e.g., "Created file 'test_file_3.py'"* | file_state | Yes | Ephemeral noise (should not be in long-term memory) |
| | | | |

- **Noise Rate (promoted ephemeral facts / total promoted):** ___ % (Target: <= 5%)

---

## 2. Memory Adapter Compatibility & Integration

Test registration, health checks, and CRUD behavior of configured memory engines.

- **Configured Engine Type:** `<e.g., cth_mcp_memory / mem0 / zep / generic_http>`

- [ ] **Prerequisites:** Promotion requires `PROMOTION_ENABLED=true` (currently `false` by default). Set it before running this section; reset after.
- [ ] **Config Validation:** Verify that `MemoryEngineConfig` fields are validated and reject empty base URLs.
- [ ] **Capabilities Check:** Verify the adapter reports capabilities correctly (e.g. `supports_batch`, `supports_search`).
- [ ] **Dry-Run Compliance:** Set `PROMOTION_DRY_RUN=true`. Run a session that triggers fact promotions.
  * **Result Check:** Verify logs record promotion calls, but no writes are committed to the target memory engine.
- [ ] **Error Fallback / Retry:** Block connection to the memory engine mid-promotion.
  * **Result Check:** Confirm the promotion fails gracefully, writes a failure record to the database, and is retryable via `/promotions/retry/{id}`.

---

## 3. Context Drift & Deduplication

- [ ] **Cross-Session Retrieval Drift:** Initialize a new session with historical facts retrieved from long-term memory. Verify that:
  1. The retrieved facts are relevant to the initial session prompt.
  2. Stale or superseded facts from long-term memory do not overwrite newer local context.
- [ ] **Fact Duplication:** Verify that facts retrieved from the long-term memory database are de-duplicated against active session facts in the final assembled system message, preventing redundant token spend.

---

## Findings

### Issues Found
Severity: P1 (Data corruption / major drift) / P2 (Poor promotion criteria / noise) / P3 (Minor)

1. **[P?]** <Issue Description> — <Evidence / Sample fact>

### Recommendations
1. <Remediation recommendations>

---

## Conclusion
1-3 sentences: Is fact promotion accurately distilling session learnings into long-term memory? What is the main alignment issue?
