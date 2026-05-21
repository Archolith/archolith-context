# Open Source Launch & Grant Strategy — cth.context-engine

**Date:** 2026-05-21
**Author:** Charles Harvey + Claude
**Status:** DRAFT

---

## Current State

**What exists:**
- Working OpenAI-compatible proxy with context assembly, fact extraction, relevance scoring, recall tool, compaction, and long-term memory promotion
- Dual graph backends: Neo4j and LadybugDB (embedded, zero-infra)
- 74 commits, 16 test files, Docker + docker-compose
- Benchmark infrastructure with parallel comparison (proxy vs direct)
- Real results: 50-60% steady-state token savings with quality preserved at 4K budget
- 7 audit templates covering performance, quality, security, resilience, concurrency, memory, and product readiness
- No README (empty), no LICENSE file, no contributing guide
- No secrets in git history — .env was never committed
- **Repo just made private** (was public with no docs — low risk of prior exposure)

**What's missing for OSS launch:**
- Public identity (name, logo, tagline)
- README with benchmark results and architecture diagram
- LICENSE file
- .env.example with all config vars documented
- One-command local setup (docker compose up)
- Contributing guide
- Changelog visible to external contributors
- CI/CD (tests, linting)

---

## Hard Deadline

**Claude for Open Source application: June 30, 2026** (40 days)

This is the most time-sensitive program. Acceptance gives 6 months of Claude Max 20x ($1,200 value) and puts the project on Anthropic's radar. The "critical infrastructure" exception means we don't need 5K stars — we need a compelling project with real benchmarks.

---

## Phase 1: Identity & Naming (Week 1 — by May 28)

### Decision: Project Name
Current candidates: `liminal-engine`, `kadath`

Criteria for the name:
- Memorable, pronounceable, searchable
- Available on PyPI, npm (if needed), GitHub
- Communicates "context" or "memory" or "intelligence layer"
- Not already an established project

**Action items:**
- [ ] Check availability of top 3 name candidates on PyPI, GitHub, and domains
- [ ] Pick the name
- [ ] Reserve PyPI package name (empty placeholder upload)
- [ ] Create a new GitHub org or rename the repo under ctharvey/

### Tagline
Draft: *"Drop-in proxy that gives any LLM a memory. 50-60% token savings, zero code changes."*

---

## Phase 2: Code Cleanup (Week 1-2 — by June 4)

Goal: Make the codebase presentable to a senior engineer who finds it on GitHub.

- [ ] **Remove internal references**: Strip `cth.` prefix from imports, package name, and docs. The public project should feel like a standalone product, not a personal namespace.
- [ ] **Audit .agent/ directory**: Decide what ships publicly vs stays private. Agent docs are useful for contributors but audit reports with specific benchmark data may reveal too much about infra.
- [ ] **Clean up pyproject.toml**: Update name, description, author, URLs, classifiers
- [ ] **Create .env.example**: Every config var with description, sensible defaults, no real keys
- [ ] **Verify docker-compose works from scratch**: `git clone && docker compose up` should work in < 2 minutes with LadybugDB (no Neo4j required)
- [ ] **Run full test suite, fix any failures**: All 16 test files must pass
- [ ] **Add type hints** to any public API functions missing them
- [ ] **Remove dead code**: Unused imports, commented-out blocks, TODO placeholders that won't ship

### Files to NOT ship publicly:
- `.agent/audits/` — internal benchmark reports (cherry-pick data for README instead)
- `.agent/plans/` — internal planning docs
- `.agent/reviews/` — internal review docs
- `scripts/benchmark_results_*.json` — raw results with session IDs
- Any file referencing personal infrastructure (VPS, memory MCP, etc.)

### Files to add:
- `ARCHITECTURE.md` — public version of the architecture doc, stripped of internal details
- `BENCHMARKS.md` — curated benchmark results with charts/tables

---

## Phase 3: Documentation & README (Week 2 — by June 4)

The README is the product. It must answer in 30 seconds: "What is this, why should I care, how do I try it?"

### README structure:
1. **Hero**: Name + one-line tagline + benchmark chart (before/after token graph)
2. **What it does**: 3-bullet summary
   - Intercepts OpenAI-compatible API calls
   - Extracts facts/decisions into a knowledge graph
   - Replaces growing conversation history with compressed context
3. **Quick start**: `docker compose up` → send a request → see the savings
4. **Benchmark results**: Table showing per-turn savings from the 16-turn gpt-4o-mini run
5. **How it works**: Architecture diagram (assembly pipeline flow)
6. **Configuration**: Key env vars with descriptions
7. **Backends**: Neo4j vs LadybugDB comparison
8. **Recall Tool**: How the model can query its own knowledge graph
9. **Roadmap**: What's coming next
10. **Contributing**: Link to CONTRIBUTING.md
11. **License**: Apache 2.0

### Additional docs:
- [ ] `CONTRIBUTING.md` — how to set up dev env, run tests, submit PRs
- [ ] `ARCHITECTURE.md` — public-facing system design
- [ ] `BENCHMARKS.md` — detailed results with methodology
- [ ] `CHANGELOG.md` — public changelog (clean version)

---

## Phase 4: CI/CD & Quality Gates (Week 2-3 — by June 11)

- [ ] **GitHub Actions**: pytest on push/PR, lint with ruff, type check with mypy
- [ ] **Pre-commit hooks**: ruff format, ruff check
- [ ] **Test coverage badge**: Show actual coverage in README
- [ ] **Docker build CI**: Verify the Docker image builds on every push
- [ ] **Release workflow**: Tag → build → publish to PyPI (when ready)

---

## Phase 5: License & Legal (Week 2 — by June 4)

- [ ] **Add Apache 2.0 LICENSE file** — standard for infra tools, permissive with patent grant
- [ ] **Add license headers** to all source files (optional but professional)
- [ ] **Verify no GPL dependencies** that would conflict with Apache 2.0
- [ ] **CLA decision**: For solo developer, not needed initially. Add later if contributors appear.

---

## Phase 6: Soft Launch (Week 3 — by June 11)

Goal: Make the repo public with enough polish that someone clicking through from an application form is impressed.

- [ ] Flip repo to public
- [ ] Post on Hacker News (Show HN: Drop-in proxy that gives LLMs long-term memory — 50-60% token savings)
- [ ] Post on r/LocalLLaMA, r/MachineLearning
- [ ] Tweet/post the benchmark chart with a link
- [ ] Submit to awesome-llm lists on GitHub

### Success metrics for soft launch:
- 50+ GitHub stars in first week (realistic for a good Show HN)
- 5+ people successfully run `docker compose up`
- 1+ external contributor opens a PR or issue

---

## Phase 7: Grant Applications (Week 3-4 — by June 18)

### Application 1: Claude for Open Source (deadline June 30)
- Apply at claude.com/contact-sales/claude-for-oss
- Pitch: "Context compression proxy for LLMs — open-source infrastructure that reduces API costs 50-60% while preserving answer quality. Dual backend (embedded + Neo4j), recall tool for active context retrieval, proven with 16-turn parallel benchmarks."
- Link the public repo with README, benchmarks, and star count
- Mention the "critical infrastructure" angle — this is middleware for every LLM application

### Application 2: Claude for Startups ($25K-$100K API credits)
- Apply at claude.com/programs/startups
- Pitch angle: "Building the context layer for LLM applications. Our open-source proxy reduces token costs 50-60% and adds persistent memory to any OpenAI-compatible API. We're building a hosted version for teams that don't want to self-host."
- Include: benchmark data, architecture overview, roadmap for hosted product
- Emphasize Claude integration: "We plan to add native Anthropic API support (Messages API format) alongside the existing OpenAI-compatible interface"

### Application 3: Anthropic Research Credits ($500-$25K)
- Fallback if the above two don't land
- Frame as research: "Studying context compression, retrieval-augmented generation, and active recall in multi-turn LLM conversations"

### Timing:
- Submit Claude for Open Source first (rolling review, deadline June 30)
- Submit Claude for Startups 1-2 weeks after soft launch (with traction data)
- Submit Research Credits only if both above are rejected

---

## Phase 8: Post-Launch Roadmap (July+)

### Open Source Roadmap (public):
1. **Native Anthropic/Claude API support** — Messages API format, not just OpenAI-compatible
2. **Streaming recall** — recall tool works in streaming mode (currently non-streaming resend)
3. **Multi-session context** — cross-session fact retrieval from long-term memory
4. **Plugin system** — custom extractors, custom relevance scorers
5. **Prometheus metrics** — standard observability
6. **Helm chart** — Kubernetes deployment

### Commercial Roadmap (private):
1. **Hosted proxy service** — sign up, get an endpoint, route your traffic
2. **Dashboard** — session visualization, fact explorer, savings analytics
3. **Team features** — shared knowledge graphs across sessions/users
4. **Enterprise** — SSO, audit logs, data residency, SLAs

---

## Risk Register

| Risk | Mitigation |
|------|------------|
| Someone forks and out-executes us | Move fast, build community, be the expert. Architecture knowledge > code. |
| Anthropic has internal context management that supersedes this | Our tool works with ANY LLM, not just Claude. Position as ecosystem infrastructure. |
| LadybugDB WAL corruption scares users | Document the issue, provide a health check endpoint, auto-recovery script. Long-term: upstream fix or alternative embedded backend. |
| Solo developer burnout | Prioritize ruthlessly. OSS launch + one grant application is the MVP. Everything else is stretch. |
| No traction after launch | The benchmarks are real. If the launch flops, it's a marketing problem — iterate on positioning, not product. |

---

## Weekly Schedule

| Week | Dates | Focus | Deliverable |
|------|-------|-------|-------------|
| 1 | May 22-28 | Name + code cleanup | Name chosen, .env.example, dead code removed |
| 2 | May 29-Jun 4 | README + docs + license | README, ARCHITECTURE.md, BENCHMARKS.md, Apache 2.0 |
| 3 | Jun 5-11 | CI/CD + soft launch | Repo public, GitHub Actions, Show HN post |
| 4 | Jun 12-18 | Grant applications | Claude for OSS submitted, Startup app drafted |
| 5 | Jun 19-25 | Startup app + community | Startup app submitted, respond to issues/PRs |
| 6 | Jun 26-30 | Buffer / Claude OSS deadline | Final check, any missing items |

---

## Definition of Done

The open source launch is "done" when:
1. Repo is public with README, LICENSE, CONTRIBUTING, and ARCHITECTURE docs
2. `docker compose up` works from a clean clone with LadybugDB (no Neo4j)
3. CI passes on every push (tests + lint)
4. At least one Anthropic program application is submitted
5. At least one public launch post is live (HN, Reddit, or Twitter)
