# archolith-proxy

Self-hosted context intelligence for LLMs.

Compress context. Extract knowledge. Remember everything.

`archolith-proxy` is an OpenAI-compatible proxy that sits in front of `/v1/chat/completions`, extracts durable facts from each turn, stores them in a session graph, and replaces replayed middle history with a smaller assembled context block. The goal is not generic prompt compression. The goal is preserving continuity in long-running coding and agent conversations without paying to resend every prior turn on every request.

- Product name: `archolith-proxy`
- Python package: `archolith_proxy`
- Default public bootstrap: LadybugDB + OpenAI-compatible upstream on port `9800`
- Current focus: proxy-first OSS release, with `archolith-memory` and `archolith-filter` as adjacent products on the broader roadmap

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the system walkthrough and [CONTRIBUTING.md](./CONTRIBUTING.md) for local development.

## What It Does

`archolith-proxy` keeps the parts of a conversation that still matter and rewrites the parts that no longer need verbatim replay.

At a high level it:

1. Resolves a stable session from `X-Session-ID` or a prompt fingerprint.
2. Sends the current request upstream through an OpenAI-compatible interface.
3. Extracts facts, decisions, observations, tool results, and goals after each turn.
4. Stores those artifacts in a session graph.
5. Rebuilds future requests by preserving the system prompt and recent coherence tail while replacing older middle history with graph-assembled context.

This makes the proxy useful for:

- long coding sessions where earlier design decisions still matter
- agent conversations with heavy tool output
- OpenAI-compatible clients that already know how to talk to `/v1/chat/completions`
- local or self-hosted workflows where you want to keep session memory under your control

## Quick Start

### Option A: Docker Compose

```bash
# fill in UPSTREAM_API_KEY and, for graph features, EXTRACTOR_API_KEY

docker compose up --build
```

Copy `.env.example` to `.env` before starting the container.

The default compose profile runs the proxy with:

- `GRAPH_BACKEND=ladybug`
- embedded LadybugDB at `/app/data/context.lbug`
- health check on `http://localhost:9800/live`

If you want the Neo4j backend instead:

```bash
docker compose --profile neo4j up --build
```

### Option B: Local Python Environment

```bash
uv sync --extra dev
# fill in UPSTREAM_API_KEY and, for graph features, EXTRACTOR_API_KEY

uv run uvicorn archolith_proxy.main:app --host 0.0.0.0 --port 9800
```

Copy `.env.example` to `.env` before running the proxy locally.

### Point an OpenAI-Compatible Client at the Proxy

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9800/v1",
    api_key="proxy-local",
    default_headers={"X-Session-ID": "demo-session"},
)

resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "You are helping implement a background task queue."},
        {"role": "user", "content": "Design the task model and enqueue flow."},
    ],
)

print(resp.choices[0].message.content)
```

Notes:

- The proxy forwards requests using `UPSTREAM_API_KEY`; the client-side `api_key` can be any placeholder.
- `X-Session-ID` is the most reliable way to keep turns in the same graph-backed session.
- The root URL redirects to the local dashboard at `/dashboard/dashboard.html`.

## Benchmark Results

The committed benchmark audit is [`./.agent/audits/2026-05-21-gpt4omini-16turn-baseline.md`](./.agent/audits/2026-05-21-gpt4omini-16turn-baseline.md).

That run used a 16-turn coding conversation against `gpt-4o-mini` with LadybugDB, `gpt-4.1-mini` extraction, embeddings enabled, and relaxed gating so assembly would activate earlier than the production defaults. It is a useful system audit, but it is not a claim that the default `.env.example` values will produce the same curve out of the box.

### Aggregate Outcomes

- Total direct input tokens: `138,016`
- Total proxy savings: `49,926`
- Overall savings ratio: `36.2%`
- Steady-state savings range: `45-65%` across turns `7-16`
- Peak single-turn savings: `65%` on turn `7`
- Mean response time: `32,041 ms` direct vs `18,168 ms` through the proxy in that run

### Representative Turns

| Turn | Direct In | Proxy In | Rewritten | Savings | Ratio | Mode |
|------|-----------|----------|-----------|---------|-------|------|
| 1 | 119 | 0 | 0 | 0 | 0% | `unknown` |
| 4 | 2,743 | 1,258 | 1,106 | 152 | 12% | `graph` |
| 8 | 7,671 | 4,750 | 2,598 | 2,152 | 45% | `graph` |
| 12 | 13,194 | 8,971 | 4,121 | 4,850 | 54% | `graph` |
| 16 | 18,370 | 12,828 | 5,206 | 7,622 | 59% | `graph` |

The main signal from the audit is not "compression starts immediately." It is that once the conversation becomes long enough to have real replay cost, the graph-assembled form grows much more slowly than raw chat history.

## How It Works

### 1. Session Resolution

The proxy first resolves a session:

- preferred: `X-Session-ID`
- fallback: a SHA-256 fingerprint of the sanitized system prompt and first user message

This lets clients opt into stable session IDs while still giving single-threaded tools a deterministic fallback.

### 2. Post-Response Extraction

After the upstream response returns, the proxy asynchronously extracts structured knowledge from the turn:

- `decision`
- `file_state`
- `tool_result`
- `state`
- `goal`
- `observation`
- `error`

Those facts are deduplicated, written to the graph backend, and associated with the session.

### 3. Context Assembly

On later turns, the assembler builds a replacement context block from graph facts instead of replaying every prior message.

The assembled block has two layers:

- `SESSION OVERVIEW`: current goal, touched files, decisions, fact count
- `RELEVANT CONTEXT`: ranked facts selected by recency, confidence, type, and optional embedding similarity

The proxy then:

- preserves the original system prompt
- preserves a smart "coherence tail" of recent messages
- removes the replay-heavy middle
- merges the assembled context into the system message so providers that dislike consecutive system messages still work

### 4. Optional Recall Tool

If `SESSION_RECALL_TOOL_ENABLED=true`, the proxy injects a hidden `__archolith_recall` tool into the request. If the model calls it, the proxy intercepts the tool call, queries the session graph, returns a bounded tool result, and re-sends the request upstream. The tool never leaks back out as part of the public API response.

### 5. Optional Long-Term Promotion

If `PROMOTION_ENABLED=true`, high-confidence session facts can be promoted into an external memory backend. The registry currently supports adapters including `archolith_memory`, `mem0`, `zep`, `generic_http`, `basic_memory`, `claude_mem`, `cognee`, `openmemory`, and `nocturne_memory`.

## Session Recall Tool

The recall tool is intentionally conservative:

- it is off by default
- it only searches the current session graph
- it rewrites ambiguous recall queries only when query rewriting is enabled
- it caps returned recall context to an internal token budget
- it is removed from the final response payload after interception

This makes it useful for targeted "what did we decide earlier?" lookups without turning every request into a tool-using workflow.

## Configuration

Start with `.env.example`. Only `UPSTREAM_API_KEY` is required for passthrough proxying, but graph-backed features need more than that.

| Variable | Purpose | Default / Recommended |
|----------|---------|-----------------------|
| `UPSTREAM_BASE_URL` | OpenAI-compatible upstream endpoint | `https://api.openai.com/v1` in `.env.example` |
| `UPSTREAM_API_KEY` | Credential used for proxied upstream calls | required |
| `GRAPH_BACKEND` | Session graph backend | `ladybug` for getting started |
| `LADYBUG_DB_PATH` | Embedded graph file path | `./data/context.lbug` |
| `SESSION_NEO4J_*` | Neo4j connection settings | only needed with `GRAPH_BACKEND=neo4j` |
| `EXTRACTOR_MODEL` | Post-turn fact extraction model | `gpt-4.1-mini` |
| `EMBEDDING_MODEL` | Similarity search model | `text-embedding-3-small` |
| `COHERENCE_TAIL_SIZE` | Recent messages preserved verbatim | `10` |
| `CONTEXT_TOKEN_BUDGET` | Budget for the assembled context block | `15000` |
| `COLD_START_TURNS` | Minimum turns before assembly is considered | `3` |
| `ASSEMBLY_MIN_SAVINGS_RATIO` | Skip rewriting if savings are too small | `0.20` |
| `ASSEMBLY_MIN_INPUT_TOKENS` | Skip rewriting below this input size | `50000` |
| `SESSION_RECALL_TOOL_ENABLED` | Enable hidden recall tool injection | `false` |
| `PROMOTION_ENABLED` | Enable long-term memory promotion | `false` |
| `ADMIN_TOKEN` | Protect operator surfaces like `/trace` and `/sessions` | empty by default |

Current code-path nuance: if you enable extraction, query rewriting, or embeddings, set `EXTRACTOR_API_KEY` and `EMBEDDING_API_KEY` explicitly. The comments in `.env.example` describe fallback behavior, but the active code paths check the explicit values.

## Backends

### LadybugDB

LadybugDB is the default bootstrap backend because it is embedded and file-backed:

- no external database server
- good fit for local development and single-node deployments
- schema created on first connect
- stores sessions, facts, files, and decisions in one local database file

### Neo4j

Neo4j remains the more traditional graph backend:

- external service
- useful if you already operate Neo4j
- selected with `GRAPH_BACKEND=neo4j`
- requires `SESSION_NEO4J_PASSWORD`

### Long-Term Memory

Promotion is separate from the session graph. `archolith-proxy` can optionally promote facts into an external memory system, but that path is disabled by default.

## Comparison

This is a directional comparison based on public product docs as of `2026-05-22`. Check vendor docs for the latest details.

| Tool | Core idea | Deploy model | Best fit | How `archolith-proxy` differs |
|------|-----------|--------------|----------|-------------------------------|
| `archolith-proxy` | Rewrite replay-heavy chat history into a graph-assembled session context | Self-hosted proxy | Long-running coding and agent sessions where continuity matters | It sits directly in front of OpenAI-compatible chat APIs and reconstructs context from extracted facts |
| [Graphiti / Zep](https://help.getzep.com/zep-vs-graphiti) | Graphiti is an open-source temporal graph framework; Zep is a managed context platform built around it | OSS framework plus managed platform | Teams that want a graph foundation or a turnkey hosted context stack | `archolith-proxy` is narrower and proxy-first: it rewrites chat payloads for existing clients instead of being a full managed context platform |
| [Headroom](https://headroomlabs.ai/) | Compress tool output, logs, files, and RAG payloads before they hit the model | Local/open-source layer | Tool-heavy agents where non-chat payloads dominate token spend | `archolith-proxy` focuses on session memory and decision recall across turns, not just compressing the current payload |
| [The Token Company](https://thetokencompany.com/docs) | Hosted API that compresses prompt text before inference | Hosted API + SDKs | Teams that want API-based prompt compression without self-hosting | `archolith-proxy` keeps session state local and rebuilds history from graph facts instead of sending prompts to a third-party compression API |

## Roadmap

- tighten the default production tuning based on longer benchmark runs
- harden trace timing and extraction observability
- ship clearer client examples and end-to-end demos
- stabilize the proxy-first OSS release
- connect the broader `Archolith` family (`archolith-memory`, `archolith-filter`) without collapsing everything into one binary

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](./CONTRIBUTING.md) for local setup, test commands, and repo conventions.

## License

Apache 2.0. See [LICENSE](./LICENSE).
