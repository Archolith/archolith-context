# WRAPUP - archolith-context outstanding-issues remediation v3

**Date:** 2026-06-20  
**Agent:** Codex  
**Model:** GPT-5 Codex (Codex desktop)  
**Status:** PARTIAL  
**Plan / Ticket:** `C:\Users\thron\IdeaProjects\projects\archolith\.agent\plans\archolith-context-outstanding-issues-remediation-plan-v3.md`  
**Worktree:** `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context`  
**Branch:** `main`  
**Commits:** `8bc2fe9`, `2fc016f`, `c3bb594`, `b0f5e7f`, `1596dec`, `8151e34`, `f711e68`, `612ffaf`, `da9a6bb`, `6d1c037`, `c8b25e9`; related repos: `b2189ee`, `112641e`, `2f2984a`, `0a56b76`  
**Verification Scope:** `archolith-context` commits `8bc2fe9..c8b25e9`; `archolith-compliance` commit `b2189ee`; `archolith-bench` commits `2f2984a`, `0a56b76`; umbrella repo commit `112641e`  
**Docs Updated:** `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\README.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.agent\architecture.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.env.example`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.agent\for-review\WRAPUP-2026-06-20-outstanding-issues-remediation-v3.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\.agent\benchmark-notes\token-estimator-validation-2026-06-20.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\results\token-estimator-2026-06-20.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\README.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\architecture.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\data_models.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\workflows\code_conventions.md`  
**Changelog Updated:** `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.agent\CHANGELOG.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\.agent\CHANGELOG.md`, `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\CHANGELOG.md`

---

## Before Writing

The plan was worked backwards from the required end state:

1. Wrapup artifact exists at the plan-required location and is anchored to committed work.
2. Wave D cleanup is present: MAINT-07, ARCH-04, PERF-07, PERF-02.
3. Wave C coverage and benchmark work is present: CORR-07, COV-01, COV-03, COV-07.
4. Wave B correctness and compliance adoption is present: CORR-08, COMP-06, COMP-05, COMP-07.
5. Wave A prerequisite project exists: `archolith-compliance`.

All implementation items are represented by commits. The status is still `PARTIAL` because the required
`artifact_validate` tool is unavailable in this session, full-repo ruff has pre-existing failures outside this
plan, and the file-size gate is not clean.

## Summary

Implemented the v3 remediation plan across the intended project boundaries. `archolith-compliance` now exists
as a shared compliance primitive package. `archolith-context` removes the benchmark session override API,
adds structured-log redaction, adds consent-gated trace writes, adds session storage/deletion admin endpoints,
adds missing WebSocket/upstream retry/curator tests, records curator phase latency metrics, merges session
config overlays through a backend method, bounds the reconciled-session cache, and removes the dead stream
retry helper.

`archolith-bench` now uses `cl100k_base` via `tiktoken` when available, keeps the old heuristic only as a
fallback, and includes the plan-required token-estimator suite and report at
`C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\results\token-estimator-2026-06-20.md`.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\CHANGELOG.md` | New compliance project changelog. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\README.md` | New compliance project agent docs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\architecture.md` | Compliance project architecture. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\data_models.md` | Compliance primitives data models. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.agent\workflows\code_conventions.md` | Compliance project workflow conventions. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\.gitignore` | New package ignores. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\AGENTS.md` | New package coding-agent instructions. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\pyproject.toml` | New hatchling package metadata. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\src\archolith_compliance\__init__.py` | New shared compliance package. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\src\archolith_compliance\consent.py` | Consent state primitives. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\src\archolith_compliance\redact.py` | PII redaction primitives. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\src\archolith_compliance\retention.py` | Protocol-based deletion report/deleter. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\tests\test_consent.py` | Consent tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\tests\test_redact.py` | Redaction tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance\tests\test_retention.py` | Retention tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\.gitignore` | Ignores the new nested compliance repo from the umbrella repo. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\.agent\CHANGELOG.md` | CORR-07 changelog. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\.agent\benchmark-notes\token-estimator-validation-2026-06-20.md` | Initial estimator validation note. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\.gitignore` | Ignores local pytest temp/cache dirs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\archolith_bench\core\metrics.py` | Replaces bench char-count estimator with optional cl100k estimator. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\archolith_bench\suites\token_estimator.py` | Plan-required token-estimator validation suite. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\results\token-estimator-2026-06-20.md` | Plan-required acceptance report. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench\tests\test_token_estimator.py` | Estimator fixture and suite-report tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.agent\CHANGELOG.md` | Per-item context changelog entries. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.agent\architecture.md` | Documents removed benchmark API, compliance, retention, metrics. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.env.example` | New compliance settings. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.agent\for-review\WRAPUP-2026-06-20-outstanding-issues-remediation-v3.md` | Closeout artifact for this remediation. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\.gitignore` | Ignores repo-local pytest temp directory. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\README.md` | Lawful-basis, retention, and consent docs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\compliance.py` | Context integration for redaction and consent. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\config\constants.py` | Session-config denylist additions. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\config\groups\compliance.py` | New compliance settings group. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\config\settings.py` | Wires compliance settings. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\curator\loop.py` | Curator phase latency recording. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\extractor\client.py` | Redacts structured extraction parse logs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\cleanup.py` | Session graph deletion. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\ladybug_backend.py` | Backend deletion and config merge support. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\ladybug_sessions.py` | Ladybug session merge/delete helpers. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\neo4j_backend.py` | Neo4j deletion and config merge support. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\protocol.py` | Backend protocol additions. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\session.py` | Neo4j session config merge helper. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\graph\session_config.py` | Shared session config merge filtering. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\main.py` | Compliance startup logging. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\metrics.py` | Curator phase latency and reconciled cache metrics. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\openai\chat.py` | Per-request session IDs and consent context. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\openai\chat_overlay.py` | Uses merge-session-config backend method. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\openai\extraction.py` | Redacts structured session-goal logs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\proxy\session.py` | Removes benchmark globals and bounds reconciliation cache. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\proxy\upstream.py` | Removes dead stream retry helper. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\routers\admin_router.py` | Session stored/deletion endpoints. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\routers\metrics_router.py` | Exposes curator phase latency metrics. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\trace\router.py` | Removes benchmark session override endpoints. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\archolith_proxy\trace\store.py` | Consent-gated writes and trace deletion/enumeration. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\pyproject.toml` | Adds optional compliance dependency extra. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\scripts\harness_benchmark.py` | Uses per-request session headers. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\scripts\scripted_benchmark.py` | Uses per-request session headers. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_compliance_redaction.py` | COMP-06 tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_curator\test_worker.py` | Scoped warning filter for COV-07. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_curator_phase_latency_metrics.py` | PERF-02 tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_graph\test_ladybug_backend.py` | PERF-07 backend tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_per_session_config.py` | Session config denylist/merge tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_proxy\test_live_stream_ws.py` | COV-01 WebSocket route tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_proxy\test_session_reconcile_cache.py` | ARCH-04 cache-bound tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_proxy\test_upstream_retry.py` | COV-03 retry tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_retention_consent.py` | COMP-05/COMP-07 tests. |
| `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context\tests\test_unit_phase4.py` | Metrics endpoint assertions. |

## Verification

- `python -m pytest -q` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance` - `PASS` - `15 passed`.
- `python -m ruff check .` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-compliance` - `PASS` - all checks passed.
- `uv pip install -e archolith-compliance` from `C:\Users\thron\IdeaProjects\projects\archolith` - `PASS` - editable package installed after cache ACL escalation.
- `python -m pytest tests\test_token_estimator.py tests\test_cost_model.py tests\test_cost_model_cache.py -q --basetemp .pytest-tmp` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench` - `PASS` - `29 passed`.
- `python -m ruff check archolith_bench\core\metrics.py archolith_bench\suites\token_estimator.py tests\test_token_estimator.py` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench` - `PASS` - all checks passed.
- `python -m pytest -q --basetemp .pytest-tmp` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context` - `PASS` - `1196 passed, 14 warnings in 55.41s`.
- Scoped item tests in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context` - `PASS` - COMP, CORR, COV, PERF, ARCH, and MAINT test groups passed during their commits.
- Scoped ruff over all touched `archolith-context` files - `PASS` - all checks passed.
- `python -m ruff check .` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context` - `FAIL` - 134 existing lint issues across untouched files.
- `bash scripts/check_file_sizes.sh` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context` - `NOT RUN` - Windows WSL has no installed distro, so `bash` cannot execute.
- PowerShell equivalent of `scripts/check_file_sizes.sh` in `C:\Users\thron\IdeaProjects\projects\archolith\archolith-context` - `FAIL` - existing oversized files remain: `archolith_proxy\main.py`, `archolith_proxy\assembler\context.py`, `archolith_proxy\curator\tools.py`, `archolith_proxy\graph\ladybug_backend.py`, `archolith_proxy\proxy\rewrite.py`, `archolith_proxy\proxy\streaming.py`, `tests\test_extractor\test_per_tool_extraction.py`.
- `artifact_validate(artifact_type="wrapups", filename="WRAPUP-2026-06-20-outstanding-issues-remediation-v3.md")` - `NOT RUN` - no `artifact_validate` tool is exposed in this session.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`

## Completion Checklist

- Plan / acceptance criteria completed: `partial`
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes`
- If uncommitted, explain why the work is still only anchored to the current worktree and why a commit was not made before wrapup: `not applicable`

## Assumptions

1. The plan's `archolith_bench/metrics/costs.py` reference was stale; the active bench estimator is `archolith_bench/core/metrics.py`.
2. The COV-03 item was coverage-first. I preserved the current retry contract instead of changing runtime behavior to match two proposed test names that implied a behavior change.
3. The COV-01 overflow route test may use a deterministic sentinel stream because Starlette `TestClient` can drain the real queue between HTTP publishes; the core `LiveStream` queue overflow mechanics already have direct tests.
4. The pre-existing full-repo ruff and file-size violations are outside this remediation scope unless a separate cleanup plan approves broad refactors.

## Risks / Gaps

1. `artifact_validate` was unavailable, so this wrapup cannot honestly be marked `READY FOR REVIEW`.
2. Full-repo `ruff check .` is not clean due pre-existing lint violations in untouched files.
3. The file-size gate is not clean. `archolith_proxy\graph\ladybug_backend.py` was already over the limit before PERF-07 and was touched by this plan; splitting it would be a broader refactor than v3 authorized.
4. `bash scripts/check_file_sizes.sh` could not run on this Windows host because WSL has no installed distro; the PowerShell equivalent was used for evidence.
5. `C:\Users\thron\IdeaProjects\projects\archolith` still has unrelated dirty workspace documentation/archive changes that predate or sit outside this remediation. They were not reverted or included.
6. The local pytest temp directory in `archolith-context` hit restrictive ACLs during cleanup. It is ignored in git and not part of the committed work product.

## Follow-Up Tasks

1. Run `artifact_validate` when the workspace-artifacts validator is available, then update this wrapup status if it passes.
2. Decide whether to authorize a separate full-repo lint cleanup for the 134 existing ruff failures.
3. Decide whether to authorize a separate file-size refactor for the oversized context files.
4. If the intended upstream retry contract is actually "raise after retryable status exhaustion" and "metric per failed retry attempt", write a new behavior-changing plan before changing `upstream_request_with_retry`.

## Notes

- MCP/tool friction: `query_structure` is referenced in docs but was not exposed; `artifact_validate` was not exposed; recursive file searches hit denied `.pytest_cache` directories; `archolith-compliance` required `git -c safe.directory=...`; uv cache ACLs required escalation; pytest temp roots required repo-local `--basetemp`; PowerShell rejected POSIX heredoc syntax; Windows `bash` routed to WSL and failed because no distro is installed.
- No push was performed.
