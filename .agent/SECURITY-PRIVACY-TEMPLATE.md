# Security & Data Privacy Audit — <Title/Session>

**Date:** YYYY-MM-DD  
**Auditor:** <Who ran the audit>  
**Commit:** <HEAD at time of test>  
**Branch:** <Branch name>  

---

## 1. Secrets & Credentials Leakage

Verify that API keys, authorization tokens, and sensitive environment variables are never leaked into logs, files, or database nodes.

| Check | Method / Command | Evidence / Notes | Pass / Fail |
| :--- | :--- | :--- | :--- |
| **Log Inspection** | Grep stdout/stderr and logs/ for `sk-` or other API key/token patterns | | |
| **Database Inspection** | Query graph backend for secrets in stored facts. Neo4j: `MATCH (f:Fact) WHERE f.content CONTAINS 'sk-' RETURN f`. LadybugDB: query via `/trace/graph/{session}/facts` and grep results. | | |
| **Error Sanitization** | Simulate upstream 401/403/429/500 responses. Check if proxy response headers/body sanitize the API keys | | |
| **Trace Store Inspection** | Verify trace JSON payloads on disk/in-memory do not contain plain credentials | | |

---

## 2. Authorization & Access Control

Verify that administrative and inspection endpoints are protected against unauthorized access.

| Endpoint | Expected Status (Unauthenticated) | Actual Status | Pass / Fail |
| :--- | :--- | :--- | :--- |
| `GET /sessions` | 401 Unauthorized | | |
| `GET /sessions/{id}` | 401 Unauthorized | | |
| `GET /trace/sessions` | 401 Unauthorized | | |
| `GET /trace/turns/{id}` | 401 Unauthorized | | |
| `GET /memory-engines` | 401 Unauthorized | | |
| `GET /promotions` | 401 Unauthorized | | |
| `GET /dashboard/` | 401 Unauthorized (or redirect/auth page) | | |

---

## 3. Database Isolation Compliance

Assert that session labels isolate multi-tenant/multi-session environments and verify the label-guard logic.

- [ ] **Session Scoping:** Verify that all graph queries (Neo4j via `repository.py`, LadybugDB via `ladybug_backend.py`) filter by `session_id`. No query should return facts from other sessions.
- [ ] **Cross-session Isolation Test:**
  1. Send a multi-turn conversation through the proxy as Session A (e.g., "Designing a React button component").
  2. Send a different conversation as Session B (e.g., "Configuring a PostgreSQL pool in Rust").
  3. Query `GET /trace/graph/{Session_B}/facts`.
  4. **Verification:** Confirm that 0% of Session A's facts are present in Session B's context or fact set.
- [ ] **Query Sanitization (Neo4j only):** Verify that `_validate_cypher()` in `repository.py` blocks injection attempts. LadybugDB uses parameterized queries via its Python API and is not vulnerable to string injection.

---

## 4. Upstream Data Privacy & Retention

Audit data flow to external/third-party LLM providers.

- **Extractor LLM Endpoint:** `<e.g., api.openai.com>`
- **Embedding LLM Endpoint:** `<e.g., api.openai.com>`
- **Upstream LLM Endpoint:** `<e.g., api.deepseek.com>`

- [ ] **PII Sanitization:** Verify that no local system usernames, environment secrets, or local file system absolute paths (e.g., `C:\Users\<username>\...`) are forwarded to the extractor model.
- [ ] **Data Opt-Out Compliance:** Verify that LLM API accounts used for extraction and embeddings have data training opted-out.

---

## Findings

### Observations
1. <Observation>

### Vulnerabilities Found
Severity: High (Action required before deployment) / Medium (Should fix soon) / Low (Minor)

1. **[Severity]** <Issue> — <Evidence / Remediation>

---

## Conclusion
1-3 sentences: Is the proxy secure for multi-tenant or local deployment? What is the most critical vulnerability to address?
