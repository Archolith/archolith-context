# Open Source Infrastructure Launch Plan — Archolith

**Date:** 2026-05-21 (revised)
**Author:** Charles Harvey + Claude
**Status:** COMPLETE
**Revision:** 7 — final; name Archolith, namespace secured, migration plan written, design direction established

---

## Strategic Pivot (v2)

After competitive analysis, the strategy shifted from open-core (OSS proxy + proprietary hosted service) to **pure open-source infrastructure**. Key findings:

- **Zep (YC W24, $500K)** deprecated their self-hosted Community Edition in April 2025, going cloud-only. The gap: no open-source, self-hosted, full-stack agent memory system with automatic extraction exists anymore.
- **The Token Company (YC W26)** does ML-based token stripping — fast and cheap but doesn't build knowledge. Different approach entirely.
- **Headroom** does context compression via summarization — no knowledge graph, no memory.
- **Graphiti** (Zep's OSS component) is a raw graph engine — no proxy, no session management, no extraction pipeline, requires Neo4j.

**Our unique position:** The only self-hosted, transparent proxy that extracts knowledge into a graph and compresses conversation history — with an embedded backend requiring zero infrastructure. Plus a recall tool that lets the model query its own knowledge graph mid-conversation (nobody else has this).

**Three products under one brand (shipped in order):**
1. `archolith-proxy` — transparent context compression proxy (this launch)
2. `archolith-memory` — long-term cross-session knowledge graph (Phase 2)
3. `archolith-filter` — domain-aware tool output compression, ported from RTK/reasonix (Phase 3, pending benchmarks)

**What dropped:** Commercial roadmap (hosted service, dashboard, enterprise features, Claude for Startups application). This is an infrastructure play for builders, not a product for end users.

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
- Repo is private on GitHub

**Companion projects (ship later under same brand):**
- **`archolith-memory`** (Phase 2) — cth.mcp.memory, long-term knowledge graph (Neo4j + Graphiti, 283 files, 60+ tests). Together with the proxy, forms a complete Zep Cloud alternative.
- **`archolith-filter`** (Phase 3) — RTK output filtering, ported from reasonix fork (7,000+ lines TS, 87 tests). Domain-aware structural compression for git diffs, file listings, JSON, logs. Needs Python port and benchmarking to validate savings before committing to launch. Can integrate with the proxy or run standalone.

**Brand assets secured:**
- PyPI: `archolith`, `arcolith` (defensive spelling)
- GitHub: `archolith` organization
- Domain: `archolith.dev` — registered

---

## Hard Deadline

**Claude for Open Source application: June 30, 2026** (40 days from plan creation)

This is the only grant target. Acceptance gives 6 months of Claude Max 20x ($1,200 value) and puts the project on Anthropic's radar. The "critical infrastructure" angle: we're building the open-source memory layer for LLM applications that Zep abandoned.

---

## Phase 1: Identity & Naming (Week 1 — by May 28)

### Decision: Project Name — ARCHOLITH ✓

**Name:** Archolith (Rimworld's "archotech" — advanced alien technology + Greek "lithos" — stone)
**Decided:** 2026-05-21

**Pronunciation:** AR-koh-lith (3 syllables, hard consonants, lands with authority)
**Meaning:** "Ancient knowledge carved in stone" — advanced technology preserving knowledge in durable form

**Why Archolith:**
- Invented word — zero brand conflicts anywhere on the internet (checked: PyPI, GitHub, all major TLDs, web search)
- Follows the "-lith" suffix pattern: monolith, megalith, neolithic — signals permanence and infrastructure
- Rimworld easter egg for gamers (archotech = highest tier alien technology) without being recognizable as a game reference to others
- 9 letters, 3 syllables — memorable and unambiguous pronunciation
- The metaphor fits the product: advanced technology that preserves knowledge in stone (graph)
- Spelling variant `arcolith` (without H) also reserved on PyPI as defensive namespace

**Namespace secured:**
- [x] PyPI: `archolith` v0.0.1 — https://pypi.org/project/archolith/0.0.1/
- [x] PyPI: `arcolith` v0.0.1 (defensive) — https://pypi.org/project/arcolith/0.0.1/
- [x] GitHub: `archolith` org — https://github.com/archolith
- [x] Domain: `archolith.dev` — registered

**Previous namespace (deprecated — do not use):**
- PyPI `kairn` — reserved but superseded (kairn.sh brand conflict discovered)
- GitHub `kairnai` — superseded

**Products under the brand:**
- `archolith-proxy` — transparent context compression proxy (this launch)
- `archolith-memory` — long-term cross-session knowledge graph (Phase 2)
- `archolith-filter` — domain-aware tool output compression (Phase 3)

**Tagline:** "Self-hosted context intelligence for LLMs"
**Three-beat opener:** "Compress context. Extract knowledge. Remember everything."

**Remaining action items:**
- [x] Register `archolith.dev` domain
- [x] Draft tagline
- [ ] Reserve `archolith-proxy`, `archolith-memory`, `archolith-filter` on PyPI (when ready to ship)

---

## Phase 2: Code Cleanup (Week 1-2 — by June 4)

Goal: Make the codebase presentable to a senior engineer who finds it on GitHub.

- [ ] **Remove internal references**: Strip `cth.` prefix from imports, package name, and docs
- [ ] **Audit .agent/ directory**: Remove internal audits/plans/reviews — ship only what helps contributors
- [ ] **Clean up pyproject.toml**: Update name, description, author, URLs, classifiers
- [ ] **Create .env.example**: Every config var with description, sensible defaults, no real keys
- [ ] **Verify docker-compose works from scratch**: `git clone && docker compose up` must work in < 2 minutes with LadybugDB (no Neo4j required)
- [ ] **Run full test suite, fix any failures**: All 16 test files must pass
- [ ] **Add type hints** to any public API functions missing them
- [ ] **Remove dead code**: Unused imports, commented-out blocks, TODO placeholders that won't ship

### Files to NOT ship publicly:
- `.agent/audits/` — internal benchmark reports
- `.agent/plans/` — internal planning docs (including this file)
- `.agent/reviews/` — internal review docs
- `scripts/benchmark_results_*.json` — raw results with session IDs
- Any file referencing personal infrastructure (VPS, memory MCP, etc.)

### Files to add:
- `ARCHITECTURE.md` — public version of the architecture doc
- `BENCHMARKS.md` — curated benchmark results with charts/tables

---

## Phase 3: Documentation & README (Week 2 — by June 4)

The README is the product page. It must answer in 30 seconds: "What is this, why should I care, how do I try it?"

### README structure:
1. **Hero**: Name + tagline + benchmark chart (before/after token graph)
2. **What it does**: 3-bullet summary
   - Intercepts OpenAI-compatible API calls — zero code changes
   - Extracts facts/decisions into a knowledge graph automatically
   - Replaces growing conversation history with compressed, relevant context
3. **Quick start**: `docker compose up` → send a request → see the savings
4. **Benchmark results**: Table showing per-turn savings from the 16-turn gpt-4o-mini run
5. **How it works**: Architecture diagram (assembly pipeline flow)
6. **The Recall Tool**: How the model can query its own knowledge graph (unique feature — emphasize)
7. **Configuration**: Key env vars with descriptions
8. **Backends**: Neo4j vs LadybugDB comparison — emphasize zero-infra LadybugDB
9. **Comparison**: Brief positioning vs Zep, Token Company, Headroom
10. **Roadmap**: What's coming (long-term memory integration, native Anthropic API)
11. **Contributing**: Link to CONTRIBUTING.md
12. **License**: Apache 2.0

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

---

## Phase 6: Soft Launch (Week 3 — by June 11)

Goal: Repo public with enough polish that the Claude for Open Source reviewers are impressed.

- [ ] Flip repo to public
- [ ] Post on Hacker News: "Show HN: Self-hosted LLM memory proxy — extracts knowledge, compresses context, 50-60% token savings"
- [ ] Post on r/LocalLLaMA, r/MachineLearning
- [ ] Tweet/post the benchmark chart with a link
- [ ] Submit to awesome-llm and awesome-context-engineering lists

### Positioning for launch posts:
- Lead with "Zep went cloud-only. Here's the self-hosted alternative."
- Emphasize zero infrastructure (LadybugDB) — `docker compose up` and done
- Emphasize zero code changes — transparent proxy, no SDK
- Show the recall tool — "your LLM can query its own knowledge graph mid-conversation"
- Show the benchmark chart — real numbers on real conversations

### Success metrics:
- 50+ GitHub stars in first week
- 5+ people successfully run `docker compose up`
- 1+ external contributor opens a PR or issue

---

## Phase 7: Claude for Open Source Application (Week 3-4 — by June 18)

**Target:** claude.com/contact-sales/claude-for-oss
**Deadline:** June 30, 2026

### Pitch:
"Open-source context compression proxy and knowledge graph for LLMs. Drop-in replacement for direct API calls — zero code changes, 50-60% token savings, automatic fact extraction into a knowledge graph, and a novel recall tool that lets the model query its own accumulated knowledge.

After Zep deprecated their self-hosted Community Edition in 2025, there's no open-source, self-hosted alternative for agent memory with automatic extraction. We're filling that gap with an embedded backend (no Neo4j required) and a transparent proxy that works with any OpenAI-compatible client.

Benchmarked on 16-turn conversations with gpt-4o-mini: 50-60% steady-state token savings with quality preserved. The recall tool — which lets the model actively query facts it extracted from earlier in the conversation — is a novel capability no existing tool provides."

### Supporting evidence:
- Public repo with README, benchmarks, architecture docs
- Working `docker compose up` with zero external dependencies
- CI passing (tests + lint)
- GitHub stars / community activity from soft launch

---

## Phase 8: Post-Launch Products (July+)

### Phase 8a: Long-Term Memory (`archolith-memory`)

Ship cth.mcp.memory under the same brand:

1. **Prepare cth.mcp.memory for OSS** — same cleanup as Phase 2 (strip internal refs, .env.example, docs)
2. **Create a unified `docker-compose.yml`** — brings up proxy + memory + Neo4j as one stack
3. **Document the promotion pipeline** — session facts → long-term memory
4. **Position the full stack** — "Complete Zep Cloud alternative, fully self-hosted"

### Phase 8b: Tool Output Filtering (`archolith-filter`)

Extract RTK from reasonix fork, port to Python, benchmark, and ship:

1. **Benchmark RTK savings** — run the existing TS version against real agent sessions, measure token reduction on tool outputs specifically. Determine if the savings justify a standalone product.
2. **Port to Python** — 7,000 lines TS → Python. Maintain domain-aware structure (git diff parser, JSON compressor, log filter, file listing filter).
3. **Standalone + proxy integration** — usable as a library (`pip install archolith-filter`) and as an optional proxy module (enable via config flag).
4. **Benchmark combined savings** — proxy with filter vs proxy alone vs filter alone vs direct. Four-way comparison.

### Open Source Roadmap (public):
1. **Native Anthropic/Claude API support** — Messages API format, not just OpenAI-compatible
2. **Streaming recall** — recall tool works in streaming mode
3. **Long-term memory module** — cross-session knowledge graph (Phase 8a)
4. **Tool output filtering** — domain-aware compression for agent outputs (Phase 8b)
5. **Plugin system** — custom extractors, custom relevance scorers
6. **Prometheus metrics** — standard observability
7. **Helm chart** — Kubernetes deployment
8. **Multi-tenant support** — user/group isolation (match Zep's feature)

---

## Competitive Positioning

### vs Zep Cloud
"Zep has a great knowledge graph. They also deprecated self-hosted and charge per credit. We're fully open source, self-hosted, and include a transparent proxy that works with zero code changes. Plus an embedded backend — no Neo4j required."

### vs The Token Company
"Token Company strips noise tokens with ML. Fast and cheap. But it doesn't understand your conversation — it just removes low-signal tokens. We extract structured knowledge, build a graph, and let the model query it. Their LLM forgets everything after the conversation. Ours remembers."

### vs Headroom
"Headroom compresses tool outputs and RAG chunks. Good for coding agents. We compress entire conversation histories by extracting facts and reassembling relevant context. Different layer of the stack."

### vs Graphiti (Zep's OSS component)
"Graphiti is the raw graph engine — great foundation (we use it too in our long-term memory module). But it doesn't include session management, a proxy, extraction pipeline, or recall tool. It's an engine; we're the car."

---

## Risk Register

| Risk | Mitigation |
|------|------------|
| Zep re-opens self-hosted | Our proxy + recall tool + embedded backend are still unique. Speed matters — establish the community first. |
| Token Company's "good enough" approach wins | Position for the agent/multi-turn use case where memory matters, not one-shot Q&A where compression is enough. |
| Solo developer burnout | Infrastructure play = less feature pressure. Community contributions > feature sprint. Focus on the proxy launch only — memory ships later. |
| No traction after launch | The benchmarks are real. The Zep gap is real. If launch flops, iterate positioning, try different communities (Discord servers, AI agent builders). |
| LadybugDB WAL corruption scares users | Document the issue, provide health check endpoint, auto-recovery instructions. Long-term: investigate upstream fix. |

---

## Weekly Schedule

| Week | Dates | Focus | Deliverable |
|------|-------|-------|-------------|
| 1 | May 22-28 | Name + code cleanup | Name chosen, .env.example, dead code removed |
| 2 | May 29-Jun 4 | README + docs + license | README, ARCHITECTURE.md, BENCHMARKS.md, Apache 2.0 |
| 3 | Jun 5-11 | CI/CD + soft launch | Repo public, GitHub Actions, Show HN post |
| 4 | Jun 12-18 | Grant application | Claude for OSS submitted |
| 5 | Jun 19-25 | Community + polish | Respond to issues/PRs, iterate on docs |
| 6 | Jun 26-30 | Buffer / deadline | Final check, Claude for OSS deadline |

---

## Definition of Done

The open source launch is "done" when:
1. Repo is public with README, LICENSE, CONTRIBUTING, and ARCHITECTURE docs
2. `docker compose up` works from a clean clone with LadybugDB (no Neo4j)
3. CI passes on every push (tests + lint)
4. Claude for Open Source application is submitted
5. At least one public launch post is live (HN, Reddit, or Twitter)
