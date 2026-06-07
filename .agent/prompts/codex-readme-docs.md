# Codex Task: Write README + Launch Docs for archolith-proxy

## Context

archolith-proxy is a self-hosted, open-source, transparent proxy for LLM APIs
that automatically extracts knowledge from conversations into a session graph,
then compresses growing conversation history by replacing stale middle messages
with graph-assembled context. It's a drop-in replacement for api.openai.com —
any client that accepts a base URL override works unchanged with zero code changes.

The project was previously internal as `cth.context-engine` and has been renamed to `archolith-context`. It has been fully
migrated: package namespace is `archolith_proxy`, all internal refs are stripped,
450 tests pass, Docker Compose defaults to the embedded LadybugDB backend (zero
infrastructure). It's ready for public launch documentation.

**Hard deadline:** Claude for Open Source application by June 30, 2026.
**Soft launch target:** June 11, 2026 (repo goes public, Show HN post).

## Your Deliverables

Create these 4 files in the repo root. Do NOT modify any source code.

### 1. README.md

This is the product page. It must answer in 30 seconds: "What is this, why
should I care, how do I try it?"

**Structure (follow this order exactly):**

1. **Hero section** — Project name, one-line description, badge placeholders
   for CI, PyPI version, license. Keep it tight.

2. **What it does** — 3 bullet points:
   - Intercepts OpenAI-compatible API calls — zero code changes
   - Extracts facts and decisions into a knowledge graph automatically
   - Replaces growing conversation history with compressed, relevant context

3. **Quick start** — `docker compose up` → configure .env → point your client
   at localhost:9800 → done. Show the minimal steps. Reference .env.example.

4. **Benchmark results** — A markdown table showing per-turn token savings from
   real benchmark data. Use this data from the 16-turn gpt-4o-mini benchmark
   (4K token budget):

   | Turn | Direct (tokens) | Proxy (tokens) | Savings |
   |------|----------------|----------------|---------|
   | 1 | 119 | 119 | 0% (cold start) |
   | 4 | 2,851 | 2,246 | 21% |
   | 8 | 7,143 | 5,269 | 26% |
   | 9 | 9,115 | 6,623 | 27% |
   | 12 | 13,676 | 10,163 | 26% |
   | 15 | 17,588 | 13,365 | 24% |
   | 16 | 18,795 | 14,207 | 24% |

   Note: These are the input_tokens fields (what the upstream LLM sees).
   The internal savings ratio (how much of the middle was compressed) reaches
   50-60% at steady state, but the overall input token savings is 24-27%
   because system prompt and coherence tail are always passed through.
   Add a note explaining this: "The proxy compresses the middle of the
   conversation (50-60% savings on the rewritten portion) while preserving
   the system prompt and recent messages verbatim."

5. **How it works** — ASCII art or text description of the assembly pipeline:
   - Request arrives → session identified → cold start check
   - If past cold start: query graph for facts → assemble context → replace middle messages
   - Forward curated payload to upstream
   - Stream response back unchanged
   - Async: extract facts from response, store in graph, invalidate stale facts

6. **The Recall Tool** — The model can query its own knowledge graph mid-conversation.
   When SESSION_RECALL_TOOL_ENABLED=true, the proxy injects a hidden tool
   (__archolith_recall) into the tools array. If the model calls it, the proxy
   intercepts the call, queries the session graph, injects the results as a tool
   response, and re-sends the request. The model never hits the upstream API with
   the recall call — it's fully proxy-intercepted. This is a unique feature nobody
   else has.

7. **Configuration** — Reference .env.example. Group by: Required (just
   UPSTREAM_API_KEY), Graph Backend (ladybug vs neo4j), Assembly Tuning,
   Feature Flags. Don't duplicate the full .env.example — summarize the key
   knobs and link to the file.

8. **Backends** — LadybugDB (embedded, zero-infra, recommended for getting started)
   vs Neo4j (production workloads, requires separate server or docker profile).
   Show the docker compose command for each:
   - Default: `docker compose up` (LadybugDB)
   - Neo4j: `docker compose --profile neo4j up`

9. **Comparison** — Brief positioning table:
   | | archolith-proxy | Zep Cloud | Token Company | Headroom |
   |---|---|---|---|---|
   | Self-hosted | Yes | No (cloud only since Apr 2025) | No | No |
   | Zero infrastructure | Yes (LadybugDB) | N/A | N/A | N/A |
   | Knowledge extraction | Yes (automatic) | Yes | No | No |
   | Context compression | Yes (graph assembly) | No | Yes (ML stripping) | Yes (summarization) |
   | Recall tool | Yes | No | No | No |
   | Transparent proxy | Yes | No (SDK required) | Yes | No |
   | Open source | Apache-2.0 | Deprecated OSS | No | No |

10. **Roadmap** — Brief list:
    - Native Anthropic/Claude API support
    - Long-term cross-session memory (archolith-memory)
    - Domain-aware tool output filtering (archolith-filter)
    - Streaming recall
    - Plugin system for custom extractors
    - Prometheus metrics

11. **Contributing** — Link to CONTRIBUTING.md

12. **License** — Apache-2.0

**Tone:** Technical but approachable. Write for a senior developer who found this
on Hacker News and has 2 minutes to decide if it's worth trying. No marketing
fluff. Let the benchmarks and architecture speak.

**Do NOT include:** Emojis, badges that don't exist yet (use placeholder format
like `![CI](https://...)`), screenshots, or animated GIFs.

### 2. ARCHITECTURE.md

Public-facing architecture document. This is for contributors and curious developers
who want to understand how it works before diving into code.

**Cover:**
- High-level data flow (request → session → assembly → upstream → extraction → graph)
- Component breakdown: proxy layer, assembler, extractor, graph layer, memory promotion
- Session lifecycle (create → active → expired, fingerprinting, Smart Tail)
- Graph backends (Neo4j with label-based isolation vs LadybugDB embedded)
- Token budgeting (tiktoken cl100k_base, 10% margin + 500 floor)
- Cold start logic (turns < threshold AND tokens < threshold → passthrough)
- Recall tool interception flow
- Key design decisions and why (label isolation, Smart Tail, async extraction)

**Source material:** Read these files for accurate technical details:
- `archolith_proxy/config.py` — all configuration with defaults
- `archolith_proxy/proxy/` — streaming, session, recall, tool_injection
- `archolith_proxy/assembler/context.py` — the assembly pipeline
- `archolith_proxy/extractor/` — fact extraction
- `archolith_proxy/graph/` — Neo4j and LadybugDB backends
- `archolith_proxy/memory/` — promotion to long-term memory

### 3. CONTRIBUTING.md

Standard open-source contributing guide:
- Prerequisites: Python 3.12+, uv (package manager)
- Dev setup: clone, `uv sync`, copy .env.example → .env, fill in API keys
- Running tests: `uv run pytest` (450 tests, ~8 seconds)
- Running the proxy locally: `uv run uvicorn archolith_proxy.main:app --port 9800`
- Code style: ruff (configured in pyproject.toml), line length 120
- PR process: fork, branch, test, PR against main
- What we're looking for: bug fixes, new graph backends, new memory adapters,
  documentation improvements, benchmark contributions

### 4. LICENSE

Apache License 2.0 — standard full text. Copyright 2026 Charles Harvey.

## Important Constraints

- Do NOT modify any .py files, tests, config, or docker-compose
- Do NOT create files outside the repo root (no subdirectories for docs)
- Write for the GitHub rendered markdown experience
- Keep the README under 500 lines — density over length
- ARCHITECTURE.md can be longer (up to 800 lines) since it's reference material
- All technical claims must be accurate to the actual codebase — read the source
  files listed above before writing
- The package is `archolith_proxy` (underscore), the product is `archolith-proxy` (hyphen)
- Port is 9800
- Default graph backend is LadybugDB (embedded), not Neo4j
- The extraction model is gpt-4.1-mini, not gpt-4
- Apache-2.0 license, not MIT
