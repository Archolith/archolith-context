# archolith-context

Experimental OpenAI-compatible proxy that explores replacing linear conversation replay with graph-assembled context for AI coding agents. Any harness that supports a base URL override (Reasonix, Claude Code, Aider, Cursor, etc.) works unchanged.

Instead of re-sending stale conversation history on every turn, the proxy extracts session facts and file content into a local knowledge store, then attempts to rebuild a smaller context window for each upstream API call. This remains experimental: the current system is useful for proxy, filtering, tracing, and context-quality research, but it does not yet achieve the desired long-session behavior.

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
# Set UPSTREAM_API_KEY and UPSTREAM_BASE_URL in .env.
# The default ARCHOLITH_PROFILE=mechanical enables agent-solo compression
# and output filtering (requires archolith_filter installed).
# Set ARCHOLITH_PROFILE=passthrough to skip these features.

# Run the proxy
archolith-proxy

# Or: python -m archolith_proxy.main

# Point any OpenAI-compatible client at http://localhost:9800/v1
```

By default the proxy binds to `127.0.0.1`. Docker, VM, or LAN deployments that
need network exposure must set `PROXY_HOST=0.0.0.0` explicitly and should also
set `ADMIN_TOKEN` for operator endpoints.

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

## Data Processing And Retention

archolith-context processes chat-completion requests to provide proxying, session tracing, context assembly,
file-cache recall, and extraction of session facts for long-context research. By default, processing is based on
the operator's legitimate interest in debugging and improving local agent workflows. Operators that need explicit
session consent can set `SESSION_CONSENT_REQUIRED=true`; trace-store writes then require the request header
`X-Session-Consent: opt-in`.

Retention defaults are conservative but local: graph sessions expire after `SESSION_TTL_HOURS=24`, while JSONL
trace retention is disabled unless `TRACE_RETENTION_DAYS` is set. Operators can inspect stored data with
`GET /admin/sessions/{session_id}/stored` and delete known graph and trace-store data for a session with
`DELETE /admin/sessions/{session_id}`. Structured logs redact sensitive text according to
`LOG_PII_REDACTION_LEVEL` (`truncated_32` by default).

## License

Source-available under the PolyForm Noncommercial License 1.0.0.

archolith&trade; is a trademark of Charles Harvey.
