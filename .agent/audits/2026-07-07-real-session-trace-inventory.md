# Real-Session Trace Inventory

Date: 2026-07-07
Project: archolith-context
Plan: `C:\Users\thron\IdeaProjects\projects\archolith\.agent\plans\archolith-context-consolidated-plans.md`, section 11
Command: `python scripts\real_session_inventory.py`
Status: Phase 0 inventory published; grading still pending

## Summary

- Trace files scanned: 120
- Harness metadata records indexed: 204
- JSONL parse errors: 0
- Usable candidates needing session-grade: 35
- Usable candidates by arm: `curated=22`, `mechanical=9`, `passthrough=4`
- All traces by arm: `curated=61`, `mechanical=22`, `passthrough=37`

This is enough candidate volume to avoid immediately running new sessions. The remaining Phase 0 work is grading and task-outcome correlation. The requested `archolith-session-grade` grader was not available as a callable tool in this Codex environment; only references to it were found in wrapup-review skill docs.

## Interpretation

- `usable candidate` means the trace looks like a real coding-agent session and has at least two turn records. It does not mean the task completed successfully.
- `curated` means the trace saw curator/briefing modes or background-pass records. Several curated-arm sessions still have mostly `agent_solo` or `passthrough` turns, so final grading must inspect actual behavior, not just the arm label.
- Synthetic, smoke, benchmark, aggregate, and one-turn traces were excluded from the candidate set.
- Direct harness metadata correlation is incomplete for the recent `ses_*` trace ids, so task completion must be recovered from trace summaries, harness logs, or worktree diffs before these can become final eval rows.

## Usable Candidate Sessions

| Trace | Arm | Turns | User turns | Max messages | Input tokens | Savings tokens | Cache hit/miss | Filter chars saved | Modes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `ses_12b811dfeffeKTspE5xiphh9sw.jsonl` | curated | 249 | 59 | 949 | 58201018 | 4643670 | 8638976/43574891 | 48648279 | passthrough:123, agent_solo:111, curator:15 |
| `ses_17984d722ffeGvWscRmcNg26GU.jsonl` | curated | 146 | 14 | 312 | 9208958 | 3239611 | 9110656/632900 | 0 | agent_solo_compressed:99, agent_solo:32, passthrough:5, curator:4, cold_start:3, graph:3 |
| `ses_179609bf2ffewbxnDoKCdEGEUq.jsonl` | curated | 120 | 13 | 297 | 11772989 | 6518127 | 7658240/619422 | 0 | agent_solo_compressed:97, agent_solo:10, curator:5, graph:4, cold_start:3, passthrough:1 |
| `ses_13651a593ffeU1YcGdQ2vBHMID.jsonl` | curated | 72 | 19 | 158 | 1890988 | 511110 | 199424/66439 | 16654 | agent_solo:52, passthrough:14, curator:6 |
| `ses_12ed824faffeN1R8hQh6j0WzAz.jsonl` | curated | 68 | 4 | 183 | 4287582 | 1023013 | 1988608/1815184 | 7929762 | agent_solo:63, passthrough:3, curator:2 |
| `ses_16b707151ffeTrRYB1Q5zXEvO4.jsonl` | passthrough | 65 | 2 | 149 | 5003705 | 0 | 6633728/100478 | 0 | passthrough:65 |
| `ses_1379d1f4effevZS9dn279ykOjA.jsonl` | curated | 57 | 20 | 115 | 1168435 | 477261 | 0/0 | 98472 | agent_solo:36, curator:12, passthrough:9 |
| `ses_14b9cf5b7ffegWfDZs4hBUsjDj.jsonl` | curated | 43 | 2 | 91 | 697527 | 3889 | 1246848/267837 | 600663 | agent_solo:41, passthrough:2 |
| `ses_12ee27c12ffeAjQxQUmqdwgCh8.jsonl` | curated | 38 | 4 | 123 | 1950491 | 383030 | 916480/738393 | 3700429 | agent_solo:33, passthrough:3, curator:2 |
| `ses_14dba9704ffeBIfep1Fmo1x3eu.jsonl` | mechanical | 37 | 1 | 124 | 1017704 | 662853 | 184064/195479 | 2962511 | agent_solo:36, passthrough:1 |
| `ses_14ca01b7affeX5347LQnsl60V2.jsonl` | curated | 37 | 2 | 100 | 928709 | 514551 | 826240/185146 | 2228056 | agent_solo:35, passthrough:2 |
| `ses_12ee24e31ffezR5Vit7746EsTk.jsonl` | mechanical | 23 | 1 | 118 | 805684 | 126097 | 166400/318679 | 1457139 | agent_solo:22, passthrough:1 |
| `ses_1617b5313ffewdbBfA4X22RD4j.jsonl` | curated | 22 | 2 | 100 | 1350019 | 904110 | 552832/128134 | 0 | agent_solo:19, passthrough:3 |
| `ses_1325d22e5ffe52Lc4WiiH98Qeh.jsonl` | curated | 21 | 7 | 43 | 276677 | 84680 | 572800/90223 | 1539 | agent_solo:13, curator:5, passthrough:3 |
| `ses_138b368b9ffexpgwPcUDyi4QlM.jsonl` | mechanical | 20 | 2 | 38 | 188939 | 24032 | 0/0 | 4435 | agent_solo:18, passthrough:2 |
| `ses_12edda170ffe5Y5TCqjELbk7a1.jsonl` | mechanical | 17 | 1 | 123 | 872002 | 91082 | 149888/380633 | 1514307 | agent_solo:16, passthrough:1 |
| `ses_132554297ffemNpqxq3yYiRe0E.jsonl` | curated | 17 | 7 | 35 | 196628 | 16966 | 483456/63402 | 796 | agent_solo:9, passthrough:7, curator:1 |
| `ses_1376dceb3ffeqE87afq2PuMPdT.jsonl` | curated | 14 | 5 | 28 | 151407 | 42455 | 0/0 | 276 | agent_solo:8, passthrough:4, curator:2 |
| `ses_1795da356ffedxGeYDN8m9iymz.jsonl` | mechanical | 13 | 1 | 74 | 368787 | 0 | 394368/55326 | 0 | agent_solo:12, cold_start:1 |
| `ses_1276359f7ffe276qY4HayRBI81.jsonl` | mechanical | 12 | 1 | 42 | 333240 | 39930 | 113408/74838 | 643038 | agent_solo:12 |
| `ses_16b791224ffeXdmjUDPL4onBdR.jsonl` | passthrough | 11 | 2 | 34 | 421649 | 0 | 558720/75715 | 0 | passthrough:11 |
| `ses_14c6f6af1ffePXJ9xnq3ny1dAy.jsonl` | curated | 9 | 47 | 436 | 1150622 | 220960 | 250112/741871 | 695836 | agent_solo:8, curator:1 |
| `ses_137a550edffeuHqEX8p1ihT9hW.jsonl` | curated | 9 | 2 | 16 | 71973 | 5409 | 0/0 | 84 | agent_solo:6, passthrough:3 |
| `ses_16b72c0beffeaB7ukrCNHdSxBj.jsonl` | passthrough | 8 | 2 | 27 | 235427 | 0 | 329856/81704 | 0 | passthrough:8 |
| `ses_175674b75ffeMLMNylh2JuSs4y.jsonl` | curated | 8 | 3 | 17 | 101124 | 10284 | 187904/64569 | 0 | cold_start:3, agent_solo:3, curator:1, agent_solo_compressed:1 |
| `ses_179513439ffe40kZvizfEkp1ud.jsonl` | mechanical | 5 | 1 | 23 | 90767 | 0 | 88064/25580 | 0 | agent_solo:4, cold_start:1 |
| `ses_1375e1fefffeU8tb9hZroOyE80.jsonl` | mechanical | 5 | 2 | 11 | 34724 | 0 | 0/0 | 124 | agent_solo:3, passthrough:2 |
| `ses_133631bd2ffeBJmR7z1dcD4bii.jsonl` | curated | 4 | 2 | 6 | 21657 | 0 | 52480/27330 | 1108 | passthrough:2, agent_solo:2 |
| `ses_138ca5525ffeczNPtf8qFvDM7t.jsonl` | mechanical | 3 | 2 | 6 | 15102 | 0 | 0/0 | 32 | passthrough:2, agent_solo:1 |
| `ses_138b6069affewBnO5F5rcVXgfN.jsonl` | curated | 2 | 2 | 3 | 8061 | 0 | 0/0 | 0 | passthrough:2 |
| `ses_1617fbf5affeltawkC6l5NA14B.jsonl` | curated | 2 | 2 | 3 | 6839 | 0 | 4736/25330 | 0 | passthrough:2 |
| `ses_161815325ffevks0e9xRpfz4Es.jsonl` | curated | 2 | 2 | 3 | 6839 | 0 | 23040/7032 | 0 | passthrough:2 |
| `ses_161822cfdffeRJ7s3DuHk5fMrf.jsonl` | curated | 2 | 2 | 3 | 6831 | 0 | 0/30060 | 0 | passthrough:2 |
| `ses_13b83f6dfffec7rKLT1RaiVe85.jsonl` | passthrough | 2 | 2 | 3 | 6811 | 0 | 0/0 | 0 | passthrough:2 |
| `ses_14dbabfa5ffeePlkLV0CAjc304.jsonl` | curated | 2 | 2 | 3 | 6609 | 0 | 0/29197 | 0 | passthrough:2 |

## Excluded Trace Buckets

| Reason | Count | Examples |
|---|---:|---|
| aggregate/failure log | 2 | `__no_session__.jsonl`, `curator_failures.jsonl` |
| no coding harness signal | 9 | `64e0519cae23465a.jsonl`, `953667d54af24918.jsonl`, `a824d565f7aa4408.jsonl`, `baseline_real_full.jsonl`, `cd1aa0af203942ad.jsonl` |
| synthetic/smoke/benchmark naming | 69 | `bench-debugging-proxy_plus_filter-702bc28ca41a.jsonl`, `bench-long_agent-proxy_plus_filter-08bbca7630e4.jsonl`, `bench-long_agent-proxy_plus_filter-0b47a110f9a4.jsonl`, `bench-long_agent-proxy_plus_filter-0d6cc5ca3413.jsonl`, `bench-long_agent-proxy_plus_filter-35688f1bfb95.jsonl` |
| too few trace turns | 5 | `0c92d05bbf2a473d.jsonl`, `led-full-kanban.jsonl`, `s1.jsonl`, `ses_16b7e13ebffes4mH9HYfhocC3H.jsonl`, `wafer-smoke-2.jsonl` |

## Next Work

1. Grade the highest-signal candidates first: longest curated sessions, then mechanical sessions with comparable turn counts, then passthrough baselines.
2. Recover task completion evidence for each graded session from harness logs or worktree diffs before using it in the final matrix.
3. Keep new session runs blocked until grading proves which arms still have gaps.
