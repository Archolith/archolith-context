# Graph-Backed Context Engine for Coding Agents

**Status:** Technical Draft  
**Author:** Charles Harvey  
**Date:** 2026-05-09  
**Working title:** `cth.context-engine`  
**Name candidates:** `liminal-engine` · `kadath`

---

## Problem

Coding agents manage context as a linear append-only log. Every API call re-sends
the full history. A 30-turn session accumulates 100–200K input tokens; the
dominant cost is re-transmitting stale context the model never attends to.

## Prior Art (and why none solve this)

| Approach | Examples | Limitation |
|----------|----------|------------|
| Linear compression | ACON, LangChain Deep Agents | Still linear; still re-sent each turn |
| Dedup/structural | sqz, Morph FlashCompact | Tool output only; conversation untouched |
| Tool spec pruning | SkillReducer | Per-call savings 2–5K; doesn't scale |
| Code knowledge graph | code-review-graph, CodeGraph | Read-only navigation; no session state |
| Cross-session memory | Zep/Graphiti, Mem0, Letta | Additive; doesn't replace in-session context |

## What's Novel

**No shipping coding agent uses a temporal knowledge graph as the primary
in-session context store with a cheap auxiliary model performing continuous
curation, delivered as a harness-agnostic proxy.**

This proposal:
1. A cheap model (gpt-4.1-mini, <$0.02/session) extracts facts after each turn
2. Facts live in a temporal knowledge graph with validity windows
3. Per-turn context is *assembled by relevance query*, not linear replay
4. Delivered as an OpenAI-compatible proxy — works with any harness unchanged
5. The conversation log is preserved as a debug artifact, not the API payload

## Architecture: Transparent Proxy

```
┌─────────────────────────────────────────────────────────────┐
│  Any Harness (Reasonix, Claude Code, Aider, Cursor, etc.)   │
│  Points baseUrl at proxy. No code changes needed.           │
└─────────────────────────────┬───────────────────────────────┘
                              │ POST /v1/chat/completions
                              │ (full linear message history)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Context Engine Proxy (OpenAI-compatible)                     │
│                                                              │
│  ON REQUEST:                                                 │
│  1. Identify system prompt → pass through                    │
│  2. Identify last 2–3 messages → pass through (coherence)    │
│  3. Middle N messages → replace with graph-assembled context  │
│     • Query: relevant facts, active files, goal, decisions   │
│     • Budget: ~8–20K tokens (vs 80–150K incoming)            │
│  4. Forward curated payload to real API                       │
│                                                              │
│  ON RESPONSE:                                                │
│  5. Return full response to harness (unchanged)              │
│  6. Async: extract facts from response + tool results        │
│  7. Store in session-scoped graph with temporal edges         │
│  8. Invalidate superseded facts                              │
└─────────────────────────────┬───────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
┌──────────────────┐ ┌───────────────┐ ┌──────────────────┐
│ Real API Backend │ │ Fact Extractor│ │ Session Graph DB │
│ (DeepSeek, etc.) │ │ (gpt-4.1-mini)│ │ (Neo4j, separate │
│                  │ │               │ │  from long-term  │
│                  │ │               │ │  memory)         │
└──────────────────┘ └───────────────┘ └──────────────────┘
```

### Harness configuration (one env var):

```bash
# Any tool that supports base URL override:
DEEPSEEK_BASE_URL=http://localhost:9800   # Reasonix
ANTHROPIC_BASE_URL=http://localhost:9800  # Claude Code
OPENAI_BASE_URL=http://localhost:9800     # Aider/generic
```

## Isolation: Separate Graph, Not Polluted Memory

**Critical constraint:** session context must NOT contaminate long-term memory.

The existing cth.mcp.memory system uses Neo4j + Graphiti for durable cross-session
knowledge. The context engine operates in the **same Neo4j instance with label-based
isolation** (`:ContextSession` label on all session nodes, `:Memory` label on all
long-term memory nodes):

| Concern | Long-term memory (cth.mcp.memory) | Session context (context engine) |
|---------|-----------------------------------|----------------------------------|
| Neo4j database | `neo4j` (default, `:Memory` label) | `neo4j` (same DB, `:ContextSession` label) |
| Isolation | Label-scoped queries | Label-scoped queries + label-guard repository |
| Lifecycle | Permanent, decays over months | Ephemeral, TTL per session |
| Content | Decisions, corrections, structure | Tool results, file reads, errors |
| Write path | Agent stores explicitly | Proxy extracts automatically |
| Read path | `recall_memories`, `build_context` | Proxy assembles per-turn |

Neo4j Community Edition supports only one active user database. Label-based isolation
provides logical separation without requiring Enterprise:
- All queries are label-scoped — no accidental cross-contamination
- A label-guard repository layer auto-injects `:ContextSession` into every query, raising `LabelGuardViolation` for unlabeled queries
- Session data can be bulk-dropped by label (`MATCH (n:ContextSession) DETACH DELETE n`)
- Different retention policies (sessions expire after 24–72h; memory persists)
- **Migration path:** if Enterprise or a second Neo4j container is provisioned later, only the driver config changes — no query changes needed

### Optional promotion path

Some session facts *should* become long-term memory (e.g., "user prefers X
architecture pattern" discovered mid-session). A promotion gate — either
user-triggered or confidence-scored — can selectively copy facts from the session
graph into the memory graph. This is a one-way valve, not a shared namespace.

```
Session Graph ──[promote]──► Long-term Memory Graph
   (ephemeral)                  (durable, curated)
```

## Why This Isn't RAG

Naive RAG embeds messages and retrieves by similarity. That fails for code:

1. **Relevance is structural.** A file is relevant because it's in the import
   graph of what you're touching, not because it's semantically similar.
2. **Facts supersede.** "Build fails with X" is irrelevant after you fix it.
   Temporal edges handle invalidation; vector stores don't.
3. **Assembly is multi-hop.** Goal (15 turns ago) + modified files (5 turns ago) +
   current error (this turn) + dependency graph. Graph traversal composes these;
   flat retrieval can't.

## Cost Model

30-turn coding session:

| | Linear (current) | Graph-backed (proposed) |
|--|--|--|
| Frontier API input tokens | ~450K (cumulative) | ~60–90K (flat/turn) |
| Curation model | $0 | ~$0.01–0.02 (gpt-4.1-mini) |
| **Estimated session cost** | **$0.50–1.50** | **$0.08–0.20** |

Curation cost is negligible: gpt-4.1-mini processes ~2–5K tokens/extraction × 30
turns at nano-tier pricing. The savings from not re-sending 150K of stale context
per turn dwarf the extraction cost by 10–50×.

## Background: Existing Infrastructure

This proposal builds on an existing private system (cth.mcp.memory) that already
solves the long-term memory problem for AI coding agents. Here's what it does:

**In plain terms:** it's a long-term brain for AI agents. Normally, AI coding
assistants forget everything between sessions. This system gives them persistent
memory — facts, decisions, corrections, and code structure — stored in a knowledge
graph that survives across conversations.

**How it works:** Facts are nodes in a graph. Relationships connect them — "file A
imports file B", "decision X supersedes decision Y." Each fact has a timestamp for
when it became true and when it stopped being true. When an agent needs context, it
traverses the graph rather than searching a keyword index. "What's relevant to
editing this file?" follows edges: this file → imports these → last modified here →
by this decision → for this reason. Multi-hop reasoning that flat search can't do.

**What makes it work:**
- **Temporal** — facts have lifespans and decay in relevance over time
- **Self-maintaining** — a background scheduler enriches facts, resolves
  contradictions, and garbage-collects stale knowledge automatically
- **Cheap** — all LLM work (extraction, summarization, conflict resolution) runs
  on gpt-4.1-mini at fractions of a cent per operation
- **Agent-facing** — exposed as callable tools: recall, store, query structure,
  check blast radius

**The three-tier model this proposal creates:**

| Tier | Analogy | Lifetime | What lives here |
|------|---------|----------|-----------------|
| Context window | Working memory | Single API call | What the model sees right now |
| Session graph (NEW) | Today's notepad | Hours | Tool results, file reads, in-progress state |
| Long-term memory (EXISTS) | Long-term memory | Weeks–months | Decisions, patterns, corrections, structure |

The session graph (this proposal) fills the gap between "what's in the context
window right now" and "what I learned across many past sessions." It's the
ephemeral working state of a single coding task — important for continuity within
a session, but not worth polluting durable memory with.

## Reusable Components

| Capability | Status |
|------------|--------|
| Temporal knowledge graph (Neo4j + Graphiti) | Production (cth.mcp.memory) |
| Entity/fact extraction via cheap API | Production (gpt-4.1-mini wired) |
| Code structure queries (blast_radius, imports) | Production |
| Embedding pipeline (OpenAI text-embedding-3-small) | Production |
| OpenAI-compatible proxy pattern | Proven (cth.mcp.delegate uses similar) |
| Neo4j multi-database | Supported, needs provisioning |

**Missing pieces:**
- Intra-session write loop (extract per-turn, store session-scoped)
- Context assembler (relevance query → assembled message array)
- Proxy shell (FastAPI, OpenAI-compatible endpoint)
- Session lifecycle (create on first request, TTL/cleanup)

## Open Questions

1. **Cold start.** First 3–5 turns have minimal graph state. Hybrid: pass through
   linearly until graph has enough signal, then switch to assembled context.

2. **Extraction reliability.** Conservative: over-store, let retrieval filter.
   Better to have a fact in the graph unused than to lose a fact needed later.

3. **Promotion criteria.** When does a session fact earn long-term memory status?
   Confidence threshold? User confirmation? Heuristic (mentioned 3+ turns)?

4. **Streaming.** Proxy must handle SSE streaming pass-through cleanly while
   capturing the full response for post-hoc extraction.

5. **Multi-model.** Proxy needs to be API-shape-agnostic — Anthropic messages
   format differs from OpenAI. Start with OpenAI-compatible only?

## Summary

- Linear context is the dominant cost center in coding agents (~70–80% of spend)
- A transparent proxy replaces stale history with graph-assembled context
- Cheap auxiliary model (<$0.02/session) handles extraction — far cheaper than
  the frontier tokens saved
- Session graph is isolated from long-term memory via label-based isolation (`:ContextSession` label in shared Neo4j)
- Selective promotion provides a one-way valve from session → memory
- Harness-agnostic: any tool that accepts a base URL override works unchanged
- Existing infrastructure covers ~80% of the stack
- **Novel combination: no one ships the full extract→store→query→assemble loop as
  a transparent proxy with separate session isolation**
