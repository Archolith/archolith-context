# Product Readiness Audit — <title>

**Date:** YYYY-MM-DD
**Auditor:** <who>
**Commit:** <HEAD>
**Prior Audit:** <date or "first audit">

---

## Readiness Score

**Overall: ___ / 100**

Weighted sum of category scores. A viable product needs >= 70 overall with no category below 40.

| Category | Weight | Score (0-100) | Weighted |
|----------|--------|---------------|----------|
| Core Pipeline | 25% | | |
| Context Quality | 20% | | |
| Reliability | 15% | | |
| Operability | 15% | | |
| Integration | 10% | | |
| Security | 10% | | |
| Documentation | 5% | | |
| **Total** | **100%** | | **___** |

---

## 1. Core Pipeline (25%)

Does the proxy intercept, extract, assemble, and forward correctly?

| Capability | Status | Evidence | Score |
|------------|--------|----------|-------|
| Proxy forwards to upstream | | | |
| Session creation (fingerprint) | | | |
| Fact extraction (post-response) | | | |
| Fact deduplication | | | |
| Fact invalidation / supersession | | | |
| Decision extraction | | | |
| File-touch tracking | | | |
| Goal tracking / update | | | |
| Context assembly (graph mode) | | | |
| Message rewriting (system + tail) | | | |
| Query rewriting | | | |
| Embedding retrieval | | | |
| Relevance scoring (cosine + recency) | | | |
| Context-overflow compaction | | | |
| Recall tool injection + interception | | | |
| Streaming support | | | |
| Non-streaming support | | | |

**Status key:** Working / Partial / Broken / Not built / Not tested

**Category score:** ___/100
**Blocker list:**
1. <blocker>

## 2. Context Quality (20%)

Does the assembled context contain the right knowledge? Use the Context Quality Scorecard (separate template) for detailed per-turn analysis. Summarize here.

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| Fact recall (relevant facts surfaced) | | >= 80% | |
| Fact precision (irrelevant facts excluded) | | >= 70% | |
| Decision recall | | >= 90% | |
| Context completeness (LLM answers match full-history answers) | | >= 85% | |
| Stale fact rate (superseded facts in context) | | <= 5% | |
| Mean relevance score of selected facts | | >= 0.6 | |

**Category score:** ___/100
**Blocker list:**
1. <blocker>

## 3. Reliability (15%)

Does the system degrade gracefully and handle failures?

| Scenario | Behavior | Tested | Score |
|----------|----------|--------|-------|
| Graph backend unavailable | Passthrough (no crash) | | |
| Upstream LLM returns error | Forward error to client | | |
| Upstream returns non-JSON | Handle without crash | | |
| Extraction fails | Log + continue (non-blocking) | | |
| Embedding API fails | Fall back to priority scoring | | |
| Query rewrite fails | Use original query | | |
| Compaction fails | Keep uncompacted context | | |
| LadybugDB WAL corruption | Recoverable on restart | | |
| Hard process kill | DB recovers on restart | | |
| Concurrent requests | No race conditions | | |
| Session TTL expiry | Old sessions cleaned up | | |

**Category score:** ___/100
**Blocker list:**
1. <blocker>

## 4. Operability (15%)

Can an operator deploy, monitor, and troubleshoot the system?

| Capability | Status | Evidence |
|------------|--------|----------|
| `/health` endpoint | | |
| `/live` liveness probe | | |
| `/ready` readiness probe | | |
| `/metrics` endpoint | | |
| `/trace` session inspection | | |
| Fact graph explorer (`/trace/graph/*`) | | |
| Extraction QA workbench (`/trace/qa/extract`) | | |
| Live dashboard (`/dashboard`) | | |
| Structured logging (structlog) | | |
| Admin token boundary | | |
| Config via env vars (no hardcoded secrets) | | |
| Docker / container ready | | |
| Graceful shutdown | | |

**Category score:** ___/100
**Blocker list:**
1. <blocker>

## 5. Integration (10%)

Can a real client point at this proxy and use it as an OpenAI drop-in?

| Requirement | Status | Notes |
|-------------|--------|-------|
| OpenAI-compatible `/v1/chat/completions` | | |
| Accepts standard `Authorization: Bearer` header | | |
| Passes through model, temperature, max_tokens, etc. | | |
| Streaming responses (SSE) | | |
| Non-streaming responses | | |
| Tool calls forwarded correctly | | |
| Multi-turn session continuity | | |
| Works with: Claude Code | | |
| Works with: OpenAI Python SDK | | |
| Works with: curl | | |
| Works with: other OpenAI-compatible clients | | |

**Category score:** ___/100
**Blocker list:**
1. <blocker>

## 6. Security (10%)

| Requirement | Status | Notes |
|-------------|--------|-------|
| API keys never logged in plaintext | | |
| Admin token protects operator endpoints | | |
| No secrets in committed files | | |
| `.env` in `.gitignore` | | |
| Upstream API key not exposed to client | | |
| No SQL/Cypher injection in graph queries | | |
| Rate limiting on proxy endpoint | | |
| Input validation on request bodies | | |

**Category score:** ___/100
**Blocker list:**
1. <blocker>

## 7. Documentation (5%)

| Document | Exists | Current | Notes |
|----------|--------|---------|-------|
| `.agent/architecture.md` | | | |
| `.agent/data_models.md` | | | |
| `.agent/CHANGELOG.md` | | | |
| `.agent/workflows/code_conventions.md` | | | |
| `.agent/BENCHMARK-AUDIT-TEMPLATE.md` | | | |
| Config/env reference (all knobs documented) | | | |
| Deployment guide | | | |
| API reference (endpoints, params, responses) | | | |

**Category score:** ___/100
**Blocker list:**
1. <blocker>

---

## Steps to Viable Product

Ordered list of what needs to happen before this is a product someone else can use.

### Blockers (must fix)

1. <blocker — what, why, estimated effort>

### High Priority (should fix before demo)

1. <item>

### Medium Priority (should fix before production)

1. <item>

### Nice to Have (polish)

1. <item>

---

## Delta from Prior Audit

| Metric | Prior | Current | Delta |
|--------|-------|---------|-------|
| Overall score | | | |
| Core Pipeline | | | |
| Context Quality | | | |
| Reliability | | | |
| Blockers remaining | | | |

## Conclusion

1-3 sentences: what is the system's readiness level and what is the single most impactful next step?

---

## Notes

- Score each capability honestly. "Working" means verified in a benchmark or test, not "the code exists."
- A capability that has code but no test coverage is "Not tested," not "Working."
- Blockers are things that would cause a demo or real usage to fail. Non-blockers can ship with known limitations.
- Update this audit after each major milestone. The delta table tracks velocity.
