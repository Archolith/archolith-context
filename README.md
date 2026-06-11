# archolith-context

OpenAI-compatible proxy that replaces linear conversation replay with graph-assembled context for AI coding agents. Any harness that supports a base URL override (Reasonix, Claude Code, Aider, Cursor, etc.) works unchanged.

Instead of re-sending stale conversation history on every turn, the proxy extracts durable session facts and file content into a local knowledge store, then rebuilds the minimal viable context window for each upstream API call. The goal: lower token spend, better continuity in long coding sessions, and agents that never re-read files they already know (via the file content cache and curator file-outline tools — note that the synthetic-tools-based native read intercept is deprecated and disabled by default in all documented configurations).

**Naming:** Public repo is `archolith-context`; Python package is `archolith_proxy`; PyPI dist is `archolith-proxy`.

## Install

```bash
# Proxy core (passthrough + session tracking + monitoring)
pip install archolith-proxy

# Proxy + token reduction (archolith-filter)
pip install archolith-proxy[filter]

# Proxy + waste monitoring (archolith-audit)
pip install archolith-proxy[audit]

# Full stack (filter + audit)
pip install archolith-proxy[full]
```

For local development from source:
```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
# Copy and configure
cp .env.example .env
# Set UPSTREAM_API_KEY and UPSTREAM_BASE_URL in .env

# Run the proxy
archolith-proxy

# Or: python -m archolith_proxy.main

# Point any OpenAI-compatible client at http://localhost:9800/v1
```

## Documentation

| File | Purpose |
|------|---------|
| [.agent/README.md](.agent/README.md) | Agent context and maintenance rules |
| [.agent/architecture.md](.agent/architecture.md) | System design, data flow, tech stack |
| [.agent/data_models.md](.agent/data_models.md) | Entities, DTOs, enums |
| [.agent/ROADMAP.md](.agent/ROADMAP.md) | Context quality improvement backlog |
| [.agent/CHANGELOG.md](.agent/CHANGELOG.md) | Running log of changes |

## Architecture

The proxy intercepts `POST /v1/chat/completions`, classifies each turn (user vs agent-solo), assembles context from the session knowledge graph, optionally runs a curator LLM to select relevant facts and file snippets, and forwards a curated payload to the upstream API. On response, it asynchronously extracts facts, caches file content, and invalidates superseded state.

See [.agent/architecture.md](.agent/architecture.md) for the full data flow diagram and component breakdown.

## License

Source-available under the PolyForm Noncommercial License 1.0.0.

archolith&trade; is a trademark of Charles Harvey.
