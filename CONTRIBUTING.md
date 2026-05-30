# Contributing to archolith-proxy

Thanks for taking a look at `archolith-proxy`.

This repository is still early, but it already has a substantial test surface and a fairly opinionated architecture. The fastest way to contribute useful work is to understand the proxy-first model before changing behavior.

## Development Setup

### Prerequisites

- Python `3.12+`
- [`uv`](https://docs.astral.sh/uv/)
- Docker and Docker Compose if you want the containerized path or Neo4j profile

### Local Setup

```bash
uv sync --extra dev
```

Copy `.env.example` to `.env` before running the proxy.

Then set at least:

- `UPSTREAM_API_KEY`

Set these as well if you want graph-backed extraction and retrieval:

- `EXTRACTOR_API_KEY`
- `EMBEDDING_API_KEY` when using embeddings

### Running the Proxy

```bash
uv run uvicorn archolith_proxy.main:app --host 0.0.0.0 --port 9800
```

Open the dashboard at:

- `http://localhost:9800/dashboard/dashboard.html`

### Docker Path

```bash
docker compose up --build
```

For Neo4j:

```bash
docker compose --profile neo4j up --build
```

## Test and Lint Commands

Run the main checks before submitting a change:

```bash
uv run pytest
uv run ruff check .
```

Useful narrower commands:

```bash
uv run pytest --collect-only -q
uv run pytest tests/test_proxy/test_tool_injection.py
uv run pytest tests/test_graph/test_ladybug_backend.py
```

The current suite collects `450` tests.

## Contribution Guidelines

### Prefer Verified Docs Over Invented Behavior

This repo is sensitive to configuration details. If you update docs or examples:

- verify env var names from code
- verify route names from code
- verify benchmark claims from committed audits
- do not invent fallback behavior that the implementation does not currently perform

### Keep the Proxy Contract Stable

The most important public contract is:

- OpenAI-compatible request/response behavior
- session continuity via `X-Session-ID`
- operator visibility through `/metrics`, `/trace`, `/sessions`, and `/dashboard`

Changes that affect those surfaces should include tests.

### Preserve the Architectural Intent

`archolith-proxy` is not trying to be:

- a generic memory SDK
- a hosted compression API
- a framework-specific agent runtime

It is a self-hosted proxy that turns replay-heavy chat history into assembled context. Contributions should reinforce that model rather than blur it.

### Use the Right Backend for the Task

- Use LadybugDB for easy local iteration and test-friendly graph storage.
- Use Neo4j only when you need the external graph deployment path.
- Treat long-term memory promotion as optional and separate from the core session graph.

## Pull Request Checklist

Before opening a PR:

1. Run the relevant tests.
2. Update root docs if the public behavior changed.
3. Update `.agent/CHANGELOG.md` for meaningful changes.
4. Include benchmark or trace evidence when changing assembly behavior, recall behavior, or extraction behavior.
5. Mention any config or migration impact clearly.

## Benchmark and Audit Artifacts

Useful repo artifacts when evaluating changes:

- `./.agent/audits/2026-05-21-gpt4omini-16turn-baseline.md`
- `./scripts/benchmark_parallel.py`
- `./scripts/e2e_smoke_test.py`
- `./archolith_proxy/static/dashboard.html`

If you touch token assembly or extraction behavior, it is worth checking the benchmark or trace story, not just unit tests.

## Code of Contribution

Pragmatic contributions are preferred:

- smaller, testable changes
- explicit configuration changes
- better operator visibility
- fewer magic behaviors

If you are unsure about a behavioral change, open with the architectural reasoning first.

## Contributor License Agreement

By submitting a pull request, you agree to the [CLA](CLA.md). In short:
you keep your copyright, but you grant the project owner a broad license
to use your contribution — including under commercial licenses. The project
itself is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

Every PR template includes a CLA checkbox. Please check it before submitting.
