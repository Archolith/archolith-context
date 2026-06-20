# MCP-Specific Extractor Ideas

**Status:** Reference only — not part of the per-tool extraction plan.

These extractors are user-deployment-specific: they depend on which MCP servers
a given user has wired into their agent.  The core `ToolExtractorRegistry` is
agnostic.  Any of these can be implemented as a `ToolExtractor` subclass and
registered at startup via a user-local extension point once that mechanism exists.

The per-tool extraction plan (`archolith-per-tool-extraction-plan.md`) tracks
the agnostic extractors.  Unknown MCP tools fall through to `DefaultExtractor`
in the meantime — they still get extracted, just generically.

> **Note — durable memory is now the `menhir` direction.**
> `MemoryRecallExtractor` has been promoted into the main extraction plan
> (`archolith-per-tool-extraction-plan.md` §3.3) and is no longer listed here.
> It covers `mcp__memory__recall_*` and `mcp__memory__build_context` via
> prefix-match routing in the registry.

---

## cth.mcp.delegate — `delegate_task`, `delegate_task_async`

**Why it matters:** The delegate returns a code block or explanation from a
remote LLM.  It looks like a mini assistant response — it may contain file
edits, function implementations, rationale.  The generic prompt misidentifies
this as tool output rather than work-product output.

**Shape:**
- `tool_names = ("mcp__delegate__delegate_task",)`
- LLM call with a targeted prompt: "This is output from a delegated coding
  task.  Extract what files were changed, what was implemented, and any
  design decisions stated."
- Schema: `{facts: [...], files_touched: [...], decisions: []}` only.
- Prefix: `"[delegate] "`.

**Caveat:** Async jobs (`delegate_task_async`) produce a job ID, not content —
the content arrives via `delegate_job_status`.  Handle the job-status result,
not the initial dispatch.

---

## mcp__vps — `vps_deploy`, `vps_service_logs`, `vps_smoke_test`

**Why it matters:** Deploy and log output has known structure.  Test counts,
health check pass/fail, and service restart events are extractable without
an LLM.

**Shape:**
- `tool_names = ("mcp__vps__vps_deploy", "mcp__vps__vps_service_logs", "mcp__vps__vps_smoke_test")`
- Regex first, LLM fallback (same pattern as `BashExtractor`).
- Deploy patterns: `r"deployed\s+(\S+)\s+successfully"`, exit status,
  service name from args.
- Log patterns: `r"ERROR\s+(.{0,120})"`, `r"started|stopped|restarted"`.
- Smoke test: `r"(?:pass|ok|healthy|fail|error)"` → `state` or `error` fact.
- Prefix: `"[vps] "`.

---

## mcp__sage-wiki — `sage_wiki_gateway` (read/search actions)

**Why it matters:** Wiki articles are reference material — they contain
architecture decisions, conventions, and domain knowledge that are directly
relevant to the session.  The generic prompt treats them as tool output and
extracts weakly.

**Shape:**
- `tool_names = ("mcp__sage-wiki__sage_wiki_gateway",)` — but only when
  `action` arg is `"wiki_read"` or `"wiki_search"`.  Dispatch on args inside
  the extractor.
- No LLM call for `wiki_read`: the article content becomes one or more
  `observation` facts (split by `##` heading, cap at 5 sections).
- `wiki_search` results: one `tool_result` fact per hit (title + excerpt),
  cap at 5.  Same as `WebSearchExtractor` but for internal wiki.
- Prefix: `"[wiki] "`.

---

## Extension Point (future)

The `ToolExtractorRegistry` can expose a `register_from_config()` classmethod
that reads a list of extractor module paths from settings:

```python
extractor_plugins: str = ""
# JSON array of "module.path:ClassName" strings
# e.g. '["myapp.extractors.vps:VpsExtractor"]'
```

This lets user-specific extractors be wired in at startup without modifying
`registry.py` — the core registry stays agnostic, plugins self-register.
This is a follow-on and not part of the current plan.
