# Code Conventions — archolith-context

## Style

- Python 3.11+ with `from __future__ import annotations` in all modules
- 4 spaces indent, no tabs
- 120 character max line length
- UTF-8 encoding for all source files
- Trailing commas in multi-line collections
- F-strings for display text; `%s`-style for logger calls only

## Imports

- Order: stdlib → third-party → local
- No unused imports; ruff catches these
- No wildcard imports
- Fully qualified imports preferred: `from archolith_proxy.curator import tools`, not bare `import tools`

## Naming

| Element | Convention | Example |
|---------|------------|---------|
| Packages | snake_case | `archolith_proxy`, `curator`, `openai` |
| Modules | snake_case | `agent_solo.py`, `circuit_breaker.py` |
| Classes | PascalCase | `CuratorResult`, `Neo4jBackend`, `Settings` |
| Functions | snake_case | `curate_context()`, `rewrite_messages()` |
| Constants | UPPER_SNAKE_CASE | `CURATOR_MAX_ITERATIONS`, `COHERENCE_TAIL_SIZE` |
| Private helpers | `_` prefix | `_run_extraction()`, `_build_outline()` |
| Private module members | `_` prefix | `_metrics` dict, `_dedup_trackers` |
| Enums | PascalCase enum, UPPER_SNAKE values | `FactType.FILE_STATE`, `SessionStatus.ACTIVE` |

## Types

- Builtin generics: `list[str]`, `dict[str, int]`, not `typing.List`/`typing.Dict`
- Union types: `str | None`, not `Optional[str]`
- `from __future__ import annotations` in every module
- Dataclasses with `field(default_factory=list)` for mutable defaults, never `= []`
- Pydantic models for config (`BaseSettings`) and API bodies
- `@dataclass` for internal data transfer objects; Pydantic `BaseModel` for serialization

## Logging

```python
import logging
log = logging.getLogger(__name__)

log.info("Processing turn %d for session %s", turn_number, session_id)
log.error("Extraction failed for session %s: %s", session_id, e)

# Bad — string concatenation in log calls
log.info(f"Processing turn {turn_number}")
```

## File Organization

```
archolith_proxy/
├── __init__.py
├── main.py                  # FastAPI app factory, lifespan, startup
├── config.py                # Settings singleton (Pydantic BaseSettings)
├── metrics.py               # In-memory metrics registry
├── openai/                  # OpenAI API surface
│   ├── chat.py              # Main chat completion handler
│   ├── streaming.py         # SSE streaming utilities
│   ├── non_streaming.py     # Non-streaming path
│   ├── extraction.py        # Post-response extraction pipeline
│   ├── file_cache.py        # File content cache integration
│   └── helpers.py           # Shared helpers
├── proxy/                   # Proxy middleware
│   ├── agent_solo.py        # Agent-solo turn compression
│   ├── circuit_breaker.py   # Circuit breaker for synthetic tools
│   ├── rewrite.py           # Message rewriting (graph context + tail)
│   ├── synthetic_tools.py   # Injected session-recall tools
│   ├── tool_injection.py    # Native read interception
│   └── upstream.py          # Upstream request with retry
├── assembler/               # Context assembly
│   ├── context.py           # Fact scoring, budgeting, formatting
│   └── tail.py              # Smart coherence tail preservation
├── extractor/               # Fact extraction
│   ├── client.py            # Extraction LLM client
│   ├── prompts.py           # Extraction system prompts
│   ├── dedup.py             # Fact deduplication (Jaccard)
│   └── registry.py          # Per-tool extractor registry
├── curator/                 # Curator LLM pipeline
│   ├── pipeline.py          # Main curator orchestrator
│   ├── loop.py              # Tool-calling iteration loop
│   ├── tools.py             # 13 curator tool handlers
│   ├── prompts.py           # Curator system prompt
│   ├── briefing.py          # SessionBriefing schema + formatting
│   ├── state.py             # Briefing cache + session state
│   ├── result.py            # CuratorResult + AssembledContext
│   ├── schemas.py           # Tool JSON schemas
│   ├── prepper.py           # Background prepper (two-curator mode)
│   └── assembler.py         # Inline assembler (two-curator mode)
├── graph/                   # Graph backend abstraction
│   ├── protocol.py          # GraphBackend protocol
│   ├── neo4j.py             # Neo4j backend
│   ├── ladybug.py           # LadybugDB backend
│   └── trace.py             # Trace store (observability)
├── memory/                  # Long-term memory promotion
│   ├── models.py            # PromotionRecord, EngineCapabilities
│   ├── registry.py          # Memory engine registry
│   ├── promotion.py         # Promotion policy + audit
│   └── adapters/            # Concrete engine adapters
├── models/                  # Shared domain models
│   └── graph_nodes.py       # SessionNode, FactNode, FileNode, etc.
├── shared/                  # Cross-layer utilities
│   └── text_utils.py        # Outline building, normalize, tokenize, Jaccard
├── routers/                 # Operator endpoints
│   ├── admin.py             # /admin/config, /admin/shutdown
│   ├── sessions.py          # /sessions, /sessions/{id}
│   ├── trace.py             # /trace/sessions, /trace/graph
│   ├── memory_admin.py      # /memory-engines, /promotions
│   └── live.py              # WebSocket /ws/stream
└── static/
    └── dashboard.html       # Web dashboard (single-page HTML)
```

### Adding a new operator route

1. Create the router in `routers/`
2. Register it in `main.py` `create_app()`
3. Document the endpoint in `.agent/architecture.md` under Observability

### Adding a new curator tool

1. Define the tool schema in `curator/schemas.py`
2. Add the handler function to `curator/tools.py`
3. Register in `TOOL_HANDLERS` dict in `curator/tools.py`
4. Add it to `CURATOR_TOOLS` list in `curator/schemas.py`
5. Update the system prompt in `curator/prompts.py` to describe the tool

## Async Conventions

- FastAPI routes are `async def` by default
- Curator loop runs with `asyncio.wait_for(timeout=...)` hard caps
- Extraction runs async (fire-and-forget) — never blocks the response
- `asyncio.create_task()` for background work; store task references for cleanup
- Graceful shutdown in `lifespan`: cancel background tasks, close backends

## Configuration

- `Settings` class in `config.py` extends Pydantic `BaseSettings`
- All settings have `env` prefix for automatic env-var binding
- `get_settings()` singleton cached; `reset_settings()` for tests
- Runtime overrides via `GET /admin/config` and `PATCH /admin/config`
- Overrides persisted to `config_overrides.json`, reloaded on startup
- Field validators for critical values: `upstream_base_url` (must be http/https), `proxy_port` (1–65535)
- Required-vs-optional checks: `check_required_for_graph()`, `check_required_for_proxy()`

## Error Handling

- All peer integrations (archolith-filter, archolith-memory) are fail-open: `ImportError` → return input unchanged
- Graph backend failures → assembly falls back to passthrough; logged, never crashes the proxy
- Extraction failures → logged and counted in metrics; never blocks response streaming
- Upstream failures → exponential backoff with `upstream_request_with_retry()`; max retries configurable
- Circuit breaker for synthetic tools: 3 consecutive failures → 5 min cooldown; 10 total → session-lifetime disable

## Testing

```bash
# Run all tests
pytest

# Run single test file
pytest tests/test_config.py

# Run single test
pytest tests/test_config.py::test_upstream_url_validation

# Run with coverage
pytest --cov=archolith_proxy --cov-report=term-missing

# Run curator tests only
pytest tests/test_curator/

# Lint
ruff check .

# Auto-fix
ruff check --fix .
```

### Test conventions

- Tests in `tests/` directory, mirror source structure: `tests/test_curator/`, `tests/test_graph/`
- Integration tests may require a running Neo4j or LadybugDB instance
- Use `reset_settings()` in test setup to isolate config changes
- Mock external HTTP calls with `httpx` or `pytest-httpx`
- Test fixture data in `tests/fixtures/` — small, representative, no secrets
- LadybugDB tests use tempfile for isolation; clean up in teardown

## Metrics

- Process-level counters in `archolith_proxy/metrics.py` `_metrics` dict
- Reset on process restart; no persistence
- Exposed at `GET /metrics` as JSON
- Key metrics: `total_requests`, `active_sessions`, `assembly_modes`, `token_savings_estimated`, `curator_calls/fallbacks/timeouts`, `synthetic_tool_*`, `extraction_*`
- Always increment and update atomically — no read-modify-write without locking
