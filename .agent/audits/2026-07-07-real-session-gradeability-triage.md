# Real-Session Gradeability Triage

Date: 2026-07-07
Project: archolith-context
Plan: `C:\Users\thron\IdeaProjects\projects\archolith\.agent\plans\archolith-context-consolidated-plans.md`, section 11
Command: `python scripts\real_session_gradeability.py`
Status: Phase 0 candidate triage published; final grading still pending

## Summary

- Candidate sessions assessed: 35
- Ready to grade from trace evidence: 21
- Harness-blocked before outcome: 7
- Manual review before grading: 5
- Evidence gaps: 2
- Ready by arm: `curated=14`, `mechanical=6`, `passthrough=1`

This narrows the first grading pass. External harness evidence did not add passthrough-ready outcomes: two passthrough attempts were blocked by model/permission gates before outcome, while the later `20260604-2148` passthrough run remains the only passthrough success with gradeable completion evidence. The data is still not balanced enough for a verdict.

## Meaning of Status

- `ready-to-grade`: trace contains a recoverable task signal and a final/outcome response signal.
- Research/report tasks count as ready when the task asks to explore/search/study/understand and the trace contains a summary/findings/report outcome.
- `harness-blocked`: trace has a task signal, but external harness logs show the run stopped at a model, permission, or interruption gate before completion evidence.
- `manual-review`: trace has a task signal, but outcome needs confirmation from harness logs, worktree diff, or task file.
- `evidence-gap`: trace lacks either a non-generic task or final response summary.

This is not the final product grade. It is a queue for the missing `archolith-session-grade` pass or an equivalent manual review.

## Ready To Grade

| Trace | Arm | Turns | User msgs | Task seed | Last response signal |
|---|---:|---:|---:|---|---|
| `ses_12b811dfeffeKTspE5xiphh9sw.jsonl` | curated | 249 | 69 | what about the tcgdex price ingest | New container is up (6 min ago). Let me retry: |
| `ses_17984d722ffeGvWscRmcNg26GU.jsonl` | curated | 146 | 14 | i wnt you to looka t the forked opencode repo. we need to get it up to date with remote | There it is. The `sync.py build` reads the registry, knows the build command and cwd, and handles the hassle. No need to look up the path... |
| `ses_179609bf2ffewbxnDoKCdEGEUq.jsonl` | curated | 120 | 11 | yawn-agent-free-endpoint-mvp-plan.md review this plan | Done. Here's what was created: --- **Done:** 1. **High-priority TODO added** -- `Migrate all JPA entities and repositories from yawn.rip i... |
| `ses_13651a593ffeU1YcGdQ2vBHMID.jsonl` | curated | 72 | 19 | Please read .harness/TASK-gate-full-20260615.md in your current directory, complete the task described there, then delete .harness/TASK-g... | Both screens created -- `login.html` and `signup.html` follow every existing convention: same DOCTYPE/meta/title/stylesheet, `.card` conta... |
| `ses_12ed824faffeN1R8hQh6j0WzAz.jsonl` | curated | 68 | 4 | Please read .harness/TASK-phaseB-topo.md in your current directory, complete the task described there, then delete .harness/TASK-phaseB-t... | All files are clean. Here's a summary of everything created: **New files (6):** - `src/domain/models/Deck.ts` -- domain model with `id`, `... |
| `ses_16b707151ffeTrRYB1Q5zXEvO4.jsonl` | passthrough | 65 | 1 | Please read the task file at C:\Users\thron\IdeaProjects\projects\ctharvey\cth.harness\session-logs\TASK-cth-agentsmith-shared-backend-20... | **Definition of Done checklist:** 1. **Topology metadata** -- `topology` field added to registry schema and validation; applied to memory,... |
| `ses_1379d1f4effevZS9dn279ykOjA.jsonl` | curated | 57 | 21 | Please read .harness/TASK-moblead-full-v2-20260614.md in your current directory, complete the task described there, then delete .harness/... | `screens/404.html` done -- centered message, `.button` to Pulse/home. 19 screens total, all 19 pages in `screens/`. Full mobile Yawn build... |
| `ses_14b9cf5b7ffegWfDZs4hBUsjDj.jsonl` | curated | 43 | 1 | Please read .harness/TASK-bench-cmp-proxy-r3.md in your current directory, complete the task described there, then delete .harness/TASK-b... | Done. Here's what I did: 1. **Explored the repo** -- read `README.md`, `package.json`, `tsconfig.json`, `src/lib.ts`, and `src/lib.test.ts... |
| `ses_12ee27c12ffeAjQxQUmqdwgCh8.jsonl` | curated | 38 | 4 | Please read .harness/TASK-phaseB-fifo.md in your current directory, complete the task described there, then delete .harness/TASK-phaseB-f... | Now let me create all the files. Starting with the data layer additions, then the feature, then the page infrastructure. |
| `ses_14dba9704ffeBIfep1Fmo1x3eu.jsonl` | mechanical | 37 | 1 | Explore the project at C:\Users\thron\IdeaProjects\.agent\worktrees\bench-cmp-proxy-20260610 thoroughly. I need: 1. Read `.agent/README.m... | Now I have all the information. Let me compile a comprehensive report. --- Here is the complete exploration of the project at **`C:\Users... |
| `ses_12ee24e31ffezR5Vit7746EsTk.jsonl` | mechanical | 23 | 1 | Explore the codebase at C:\Users\thron\IdeaProjects\projects\forked\_phaseB\fifo to understand how browse-screen features are structured.... | Now I have a thorough understanding of the entire architecture. Here is the comprehensive summary. --- ## Comprehensive Summary: Browse S... |
| `ses_1617b5313ffewdbBfA4X22RD4j.jsonl` | curated | 22 | 2 | Please read the task file at C:\Users\thron\IdeaProjects\projects\ctharvey\cth.harness\session-logs\TASK-archolith-filter-deepseek-v4flas... | Let me read all the critical source files in parallel: |
| `ses_1325d22e5ffe52Lc4WiiH98Qeh.jsonl` | curated | 21 | 7 | Please read .harness/TASK-s5-coff-20260615.md in your current directory, complete the task described there, then delete .harness/TASK-s5-... | Done. `transactions.html` follows the same conventions -- `.list-row` with card name/date in the body and sale amount in `.row-meta`, `tra... |
| `ses_138b368b9ffexpgwPcUDyi4QlM.jsonl` | mechanical | 20 | 1 | Please read .harness/TASK-ctxqual-full-20260614.md in your current directory, complete the task described there, then delete .harness/TAS... | Done. The file `kanban.py` is written and fully verified -- all subcommands work, all error paths exit non-zero with clear stderr messages. |
| `ses_132554297ffemNpqxq3yYiRe0E.jsonl` | curated | 17 | 7 | Please read .harness/TASK-s5-con-20260615.md in your current directory, complete the task described there, then delete .harness/TASK-s5-c... | Done. Task file deleted. |
| `ses_1376dceb3ffeqE87afq2PuMPdT.jsonl` | curated | 14 | 5 | Please read .harness/TASK-recall-full-20260614.md in your current directory, complete the task described there, then delete .harness/TASK... | Done. `graded.html` -- search, scrollable list of `.graded-row` (thumb, name, grade, price), imports `graded` from `api.js`. |
| `ses_1795da356ffedxGeYDN8m9iymz.jsonl` | mechanical | 13 | 1 | Search the yawn workspace (C:\Users\thron\IdeaProjects) for: 1. Any Caddyfile or caddy config files (search with glob: **/Caddyfile*, **/... | Here is a comprehensive summary of my findings. --- ## Search Results ### 1. Caddyfile / Caddy Config Files Two Caddyfiles exist, both vi... |
| `ses_137a550edffeuHqEX8p1ihT9hW.jsonl` | curated | 9 | 2 | Please read .harness/TASK-moblead-full-20260614.md in your current directory, complete the task described there, then delete .harness/TAS... | `screens/home.html` -- done. Imports `mobile.css` and `api.js`, renders 7d/30d market-breadth card at top and scrollable set-matrix `.list... |
| `ses_14c6f6af1ffePXJ9xnq3ny1dAy.jsonl` | curated | 9 | 44 | Dos ome iterations on the yawn.frontend logo. The name should be adjusted to at least Yawn only for now | Clean. Back to `position: fixed` across the full viewport at `z-index: 0`, with `.page` content stacked above at `z-index: 1`. The heat b... |
| `ses_1375e1fefffeU8tb9hZroOyE80.jsonl` | mechanical | 5 | 1 | Please read .harness/TASK-seeded-full-20260615.md in your current directory, complete the task described there, then delete .harness/TASK... | Done. Files read, conventions confirmed, task file deleted. |
| `ses_179513439ffe40kZvizfEkp1ud.jsonl` | mechanical | 5 | 1 | In the project at C:\Users\thron\IdeaProjects\projects\yawn\yawn.rip, I need to understand the existing card search infrastructure. Pleas... | Here is a complete summary of the card search infrastructure in `yawn.rip`. --- ## 1. CardSearchRepositoryImpl.java -- `searchSimpleCards... |

## Manual Review / Evidence Gaps

| Trace | Arm | Status | Reason |
|---|---:|---|---|
| `ses_14ca01b7affeX5347LQnsl60V2.jsonl` | curated | harness-blocked | external harness log shows permission/model gate before outcome (`bench-cmp-proxy-r2`, exit -1) |
| `ses_12edda170ffe5Y5TCqjELbk7a1.jsonl` | mechanical | manual-review | failure markers present; inspect task outcome before grading |
| `ses_1276359f7ffe276qY4HayRBI81.jsonl` | mechanical | evidence-gap | no non-generic user task recovered from trace |
| `ses_16b791224ffeXdmjUDPL4onBdR.jsonl` | passthrough | harness-blocked | external harness log shows permission/model gate before outcome (`cth-agentsmith-shared-backend-20260604-2125`, exit 1) |
| `ses_16b72c0beffeaB7ukrCNHdSxBj.jsonl` | passthrough | harness-blocked | external harness log shows permission/model gate before outcome (`cth-agentsmith-shared-backend-20260604-2141`, exit -1073741510) |
| `ses_175674b75ffeMLMNylh2JuSs4y.jsonl` | curated | manual-review | single task signal; needs outcome confirmation |
| `ses_133631bd2ffeBJmR7z1dcD4bii.jsonl` | curated | harness-blocked | external harness log shows permission/model gate before outcome (`delegate-server-672401`, exit 1) |
| `ses_138ca5525ffeczNPtf8qFvDM7t.jsonl` | mechanical | manual-review | single task signal; needs outcome confirmation |
| `ses_138b6069affewBnO5F5rcVXgfN.jsonl` | curated | manual-review | single task signal; needs outcome confirmation |
| `ses_13b83f6dfffec7rKLT1RaiVe85.jsonl` | passthrough | evidence-gap | no final response summary recovered |
| `ses_14dbabfa5ffeePlkLV0CAjc304.jsonl` | curated | harness-blocked | external harness log shows permission/model gate before outcome (`bench-cmp-proxy-20260610`, exit -1073741510) |
| `ses_1617fbf5affeltawkC6l5NA14B.jsonl` | curated | manual-review | single task signal; needs outcome confirmation |
| `ses_161815325ffevks0e9xRpfz4Es.jsonl` | curated | harness-blocked | external harness log shows permission/model gate before outcome (`archolith-filter-deepseek-v4flash-remediation-20260606`, exit 1) |
| `ses_161822cfdffeRJ7s3DuHk5fMrf.jsonl` | curated | harness-blocked | external harness log shows permission/model gate before outcome (`archolith-filter-deepseek-remediation-20260606`, exit 1) |

## Next Work

1. Grade the 21 ready sessions first, but do not produce a verdict until passthrough has enough confirmed outcomes.
2. Recover outcome evidence for the 5 remaining manual-review sessions from harness logs, task files, or worktree diffs.
3. Drop the 2 evidence-gap sessions unless external evidence can identify the task/outcome.
4. Treat the 7 harness-blocked sessions as launch-process evidence, not model-quality evidence.
