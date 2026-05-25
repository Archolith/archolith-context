# archolith-context — Agent Docs

Read everything in this directory before starting work.

## Files

| File | Purpose |
|------|---------|
| `architecture.md` | System design, data flow, tech stack, config/env reference |
| `data_models.md` | Entities, DTOs, enums, converters, repository reference |
| `CHANGELOG.md` | Running log of changes, most recent first |
| `workflows/code_conventions.md` | Language-specific style and formatting rules |
| `workflows/benchmarking.md` | Benchmark suite, tuning experiments, recall analysis |
| `BENCHMARK-AUDIT-TEMPLATE.md` | Template for benchmark/performance audit reports |
| `PRODUCT-READINESS-TEMPLATE.md` | Template for product readiness audits (steps to viable product) |
| `CONTEXT-QUALITY-TEMPLATE.md` | Template for context quality scorecards (recall, precision, extraction) |
| `SECURITY-PRIVACY-TEMPLATE.md` | Template for security, data privacy, and isolation audits |
| `RESILIENCE-CHAOS-TEMPLATE.md` | Template for chaos engineering, fault-tolerance, and timeouts |
| `CONCURRENCY-LOAD-TEMPLATE.md` | Template for concurrency, memory leaks, and load audits |
| `MEMORY-ALIGNMENT-TEMPLATE.md` | Template for long-term memory promotion, adapter, and drift audits |
| `audits/` | Completed audit reports |

## Maintenance Rules

- Update `data_models.md` when any entity, DTO, or enum changes.
- Update `architecture.md` when adding services, integrations, or structural changes.
- Update the relevant workflow file when operational behavior changes.
- Add a `CHANGELOG.md` entry at the end of every session with meaningful changes.
  Format: `## YYYY-MM-DD — <short description>` with bullet points per file changed.
- Push to git regularly — at minimum at the end of each working session.
- Use conventional commit messages: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`.
- Only commit files worked on this session. Run `git diff --name-only` and `git status` before staging. Add files explicitly by path — never `git add .` or `git add -A`.
