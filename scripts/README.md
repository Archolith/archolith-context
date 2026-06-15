# scripts/

Operational and development scripts for the Archolith context-engine proxy.
All scripts are standalone Python — no extra install beyond the project venv.

```
cd cth.context-engine
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
```

---

## Operational (day-to-day)

### `proxy_restart.py` — Restart proxy with DB health check
```bash
python scripts/proxy_restart.py          # restart, check DB, fix if corrupted
python scripts/proxy_restart.py --fresh  # always start with a new timestamped DB
python scripts/proxy_restart.py --db ./data/context_20240523.lbug  # explicit DB
python scripts/proxy_restart.py --timeout 30  # wait longer for graph_ready
```
Kills any existing proxy on port 9801, inspects the LadybugDB for WAL corruption
(common after force-kills), switches to a fresh timestamped DB if needed, starts
the proxy, and polls until `graph_ready=true`. Updates `.env` if DB is switched.

This is the canonical restart path for this repo. Do not add or use an alternate
restart helper unless it launches the proxy with the same durable background-process
behavior and log capture.

### `proxy_status.py` — One-shot proxy metrics and trace inspection
```bash
python scripts/proxy_status.py metrics               # quick metrics overview
python scripts/proxy_status.py sessions              # trace sessions + user turn counts
python scripts/proxy_status.py turns <session_id>   # per-turn detail for a session
python scripts/proxy_status.py watch [N]             # poll metrics every N seconds (default 5)
```
Reads `PROXY_BASE_URL` (default `http://localhost:9801`) and `PROXY_ADMIN_TOKEN`
from env or `.env`. The `/trace/*` endpoints require the admin token.

Key field: `user_turns_by_session` in metrics — shows how many real user messages
have been seen per session (gates the cold-start threshold at `COLD_START_TURNS`).

### `live_monitor.py` — Real-time WebSocket event monitor
```bash
python scripts/live_monitor.py --port 9801
python scripts/live_monitor.py --filter assembly,extraction
```
Connects to the proxy's WebSocket stream and displays colored real-time events:
requests, assembly decisions, extraction results, recall hits. Use during active
sessions to watch the proxy pipeline as it runs.

---

## Benchmarking

### `benchmark.py` — Throughput and quality benchmark runner
```bash
python scripts/benchmark.py --model deepseek-chat --turns 5
```
Runs a multi-turn benchmark scenario through the proxy and records token savings,
assembly latency, and extraction quality. Results saved to `scripts/benchmark_results.json`.

### `benchmark_parallel.py` — Parallel benchmark runner
```bash
python scripts/benchmark_parallel.py --workers 4 --runs 16
```
Runs multiple benchmark scenarios concurrently to stress-test throughput.

### `interactive_benchmark.py` — Manual benchmark with live feedback
```bash
python scripts/interactive_benchmark.py
```
Interactive REPL for running benchmark scenarios one at a time with inline results.

### `compare_experiments.py` — Compare two benchmark result files
```bash
python scripts/compare_experiments.py results_a.json results_b.json
```
Diffs two `benchmark_results.json` files to compare model/config changes.

### `contextbench_harness.py` — Context quality harness
```bash
python scripts/contextbench_harness.py
```
Structured evaluation harness for measuring context assembly quality across
predefined scenarios.

### `scripted_benchmark.py` — Scripted harness benchmark
```bash
python scripts/scripted_benchmark.py setup --scenario scripts/scenarios/harness/config_doc.json
python scripts/scripted_benchmark.py start --proxy-worktree <path> --passthrough-worktree <path>
python scripts/scripted_benchmark.py monitor
python scripts/scripted_benchmark.py report
```
Dual-session comparison (proxy vs passthrough) through isolated git worktrees.
Filesystem checkpoints provide objective pass/fail signals for gated benchmark execution.

### `harness_benchmark.py` — Harness integration benchmark
```bash
python scripts/harness_benchmark.py
```
Benchmark runner for harness integration testing. Measures token savings, latency, and
context quality in isolated worktree sessions.

---

## Testing and Diagnostics

### `e2e_smoke_test.py` — End-to-end smoke test
```bash
python scripts/e2e_smoke_test.py
```
Quick sanity check: starts a session, sends a turn through the proxy, verifies the
response and graph state. Exits non-zero if any assertion fails.

> **Removed (2026-06-15):** `test_e2e.py`, `test_multi_turn.py`,
> `test_extraction_direct.py`, `test_phase3_e2e.py`, `test_phase3_validation.py` were
> Neo4j-era manual e2e scripts, broken since the ladybug migration and superseded by the
> pytest suite (`pytest`), `diag_pipeline.py` (extraction/pipeline smoke), and the
> archolith-bench harness (multi-turn / context-assembly validation). Recover from git
> history if needed.

### `diag_pipeline.py` — Pipeline diagnostics
```bash
python scripts/diag_pipeline.py
```
Diagnoses the full proxy pipeline step by step: graph connection, extractor,
assembler, rewriter. Reports which stages are healthy.

### `audit_extraction_quality.py` — Extraction quality audit
```bash
python scripts/audit_extraction_quality.py
```
Runs a batch of test responses through the extractor and reports precision/recall
against ground-truth annotations.

### `neo4j_diagnostic.py` — Neo4j backend diagnostic
```bash
python scripts/neo4j_diagnostic.py
```
Tests Neo4j connection, schema, and basic graph queries. Use when switching to or
troubleshooting the Neo4j backend (not needed for LadybugDB).

### `test_synthetic_tools.py` — Synthetic tools test
```bash
python scripts/test_synthetic_tools.py
```
Tests the synthetic tools system (recall_session_work, recall_files_read) that allow
agents to query session state and file access logs.

### `redundancy.py` — Redundancy analysis
```bash
python scripts/redundancy.py
```
Analyzes extracted facts for redundancy and deduplication quality.

### `opencode_export.py` — OpenCode export
```bash
python scripts/opencode_export.py
```
Exports proxy configuration and benchmark results for external OpenCode-compatible
client testing.

---

## Common Workflows

**Restart after force-kill (WAL corruption):**
```bash
python scripts/proxy_restart.py --fresh
python scripts/proxy_status.py metrics
python scripts/live_monitor.py
```

**Watch a live comparison test:**
```bash
python scripts/proxy_status.py watch 3      # poll every 3s
# In another terminal:
python scripts/live_monitor.py              # stream events
```

**Inspect a specific session after assembly fires:**
```bash
python scripts/proxy_status.py sessions                    # list sessions
python scripts/proxy_status.py turns 37ef6ba0d99c4df9     # inspect turns
```

**Run full regression before committing:**
```bash
pytest tests/ -x -q
python scripts/e2e_smoke_test.py
```
