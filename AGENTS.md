# AGENTS.md

## Project Instructions For Coding Agents

1. Before making changes, read the guidance files in `.agent/`.
2. Start with `.agent/README.md` for project workflow and conventions.
3. Use `.agent/data_models.md` for entity and schema expectations.
4. Use `.agent/architecture.md` for system design and external API context.
5. Check `.agent/workflows/` for task-specific runbooks before executing operational actions.
6. If there is a conflict between code and `.agent` docs, call it out explicitly and ask for clarification.

## Scope

These instructions apply to the entire repository.

## Build / Lint / Test Commands

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run single test file
pytest tests/test_config.py

# Run single test
pytest tests/test_config.py::test_upstream_url_validation

# Lint
ruff check .

# Auto-fix lint issues
ruff check --fix .

# Run the proxy (requires upstream API key)
python -m archolith_proxy.main
```

## Code Style

See `.agent/workflows/code_conventions.md` for full rules. Key points:

- Python 3.11+, 4 spaces indent, 120 char max line length
- Builtin generics (`list`, `dict`), `X | Y` unions, not `typing.List`/`Optional`
- `%s`-style lazy formatting for loggers
- snake_case for modules/functions, PascalCase for classes

## Project-Specific Notes

- **Naming**: Public product/repo is `archolith-context`; Python package is `archolith_proxy`; PyPI distribution is `archolith-proxy`. Older `cth.context-engine` naming still appears in some historical docs.
- The proxy default config (`Settings` class) uses Neo4j as the graph backend and DeepSeek as upstream. The README and `.env.example` are optimized for the LadybugDB + OpenAI bootstrap path. Both realities are valid.
- All peer integrations (archolith-filter, menhir/durable memory adapters) are fail-open — when absent, the proxy operates in passthrough mode.
- archolith-filter is not a `pyproject.toml` dependency. Install it alongside with `uv pip install -e ../archolith-filter`.
- Session state is ephemeral (TTL default 24h). No durable storage without memory promotion enabled.
- The proxy serves on port 9800 by default. Health check at `GET /health`.
- Runtime config tunable via `GET/PATCH /admin/config`. Overrides persisted to `config_overrides.json`.
- Treat `archolith-context` as experimental. Do not update public docs to imply solved/perfect context recall or no-reread behavior.
