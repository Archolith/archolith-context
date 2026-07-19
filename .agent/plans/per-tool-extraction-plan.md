# Per-Tool Extraction Plan

**Status:** Proposed  
**Author:** Arena Agent (based on senior-dev review)  
**Date:** 2026-07-19  
**Related Review Item:** Per-tool extraction (High Value / High Effort)  
**Goal:** Significantly improve fact extraction quality by using specialized prompts and structured output per tool type instead of a single generic extraction call.

---

## Problem Statement

The current extraction pipeline uses a **single generic LLM call** for all tool results in a turn:

1. All recent tool results (Read, Bash, Grep, Write, Edit, etc.) are concatenated into one large blob.
2. This blob is sent to one extraction prompt.
3. The LLM is asked to extract facts, decisions, files, errors, etc. from the entire mess in one shot.

This approach has several problems:

- Different tools produce fundamentally different output types.
- A generic prompt cannot optimally handle all of them.
- Quality suffers, especially on complex multi-tool turns.
- Downstream consumers (assembler, graph queries, recall) receive noisier, less structured data.

**Example of the mismatch:**

| Tool       | Output Type                     | Ideal Extraction |
|------------|---------------------------------|------------------|
| **Read**   | Large file content              | File path, symbols, outline, key sections |
| **Bash**   | Command + exit code + output    | Success/failure, test results, build errors |
| **Grep**   | Pattern matches across files    | "Symbol X appears in file Y at line Z" |
| **Write/Edit** | File path + new content     | Updated file state + diff summary |
| **General**| Mixed or unknown                | Generic fallback |

A single prompt cannot do all of these well.

---

## Goals & Success Criteria

| Goal | Target | Measurement |
|------|--------|-------------|
| Improve extraction quality | Measurable increase in fact relevance and precision | Manual QA + `trace/qa/extract` scores |
| Reduce noise in facts | Fewer low-value or malformed facts | `extraction_empties` + fact quality review |
| Enable tool-specific downstream logic | Facts carry `fact_source_tool` attribution | Graph queries + assembler behavior |
| Maintain or reduce latency | Extraction time stays within current budget | `extractor_*_latency_ms` |
| Backward compatible | Existing behavior when disabled | Feature flag |

---

## Proposed Approach

Replace the single generic extraction call with a **per-tool extraction pipeline**:

1. Classify each tool result by its tool name.
2. Route it to a specialized extraction prompt + structured output schema.
3. Merge the results from all tools.
4. Store facts with a new `fact_source_tool` attribute.

This produces higher-quality, better-structured facts while keeping the overall architecture clean.

---

## Tool-Specific Extraction Strategy

| Tool          | Specialized Goal                                      | Output Schema (JSON)                          | Fact Types Produced |
|---------------|-------------------------------------------------------|-----------------------------------------------|---------------------|
| **Read**      | Extract file path, symbols, key sections, outline     | `{path, symbols, outline, key_sections}`      | `file_state`, `observation` |
| **Bash**      | Detect test results, errors, exit codes, intent       | `{command, exit_code, success, errors, summary}` | `observation`, `error`, `verification` |
| **Grep**      | Turn matches into precise location facts              | `{pattern, matches: [{file, line, symbol}]}`  | `fact` (location) |
| **Write/Edit**| Record file modification + new state                  | `{path, action, new_state_summary}`           | `file_state` |
| **Glob/Ls**   | Record discovered files                               | `{paths: [...]}`                              | `observation` |
| **Fallback**  | Generic extraction for unknown tools                  | Freeform                                      | Generic facts |

---

## Implementation Phases

### Phase 0 — Foundation (Low Risk)
- Add per-tool extraction prompts in `extractor/prompts.py`
- Create `extractor/extractors/` directory with one file per tool (`read.py`, `bash.py`, `grep.py`, etc.)
- Add `fact_source_tool` field to the LadybugDB `Fact` schema
- Add `context_cache_mode` style config flag: `per_tool_extraction_enabled`

### Phase 1 — Routing Layer
- Modify `_collect_recent_tool_results()` (or the extraction entry point) to:
  - Group tool results by tool name
  - Call the appropriate specialized extractor
- Implement a registry/dispatcher pattern (`extractor/registry.py`)

### Phase 2 — Structured Output
- Use JSON mode (or Pydantic models) for each tool-specific extractor
- Define clear Pydantic models for each tool’s output
- Merge results into the existing fact list

### Phase 3 — Integration & Polish
- Wire into the main extraction flow (`openai/extraction.py`)
- Add metrics: `per_tool_extraction_calls`, `per_tool_extraction_failures`
- Update `trace/qa/extract` to show tool attribution
- Add tests and update documentation

---

## Technical Details

### New Files (Proposed)

```
extractor/
├── prompts.py                 # Add per-tool system prompts
├── registry.py                # Tool → extractor mapping
├── extractors/
│   ├── __init__.py
│   ├── read.py
│   ├── bash.py
│   ├── grep.py
│   ├── write_edit.py
│   └── fallback.py
```

### Schema Change

Add to LadybugDB `Fact` node:

```python
fact_source_tool: str | None = None   # "Read", "Bash", "Grep", etc.
```

### Example Specialized Prompt (Read)

```python
READ_EXTRACTION_PROMPT = """
You are extracting structured information from a file read operation.

Input:
- file_path
- content (up to 200 lines)

Output JSON:
{
  "path": "...",
  "symbols": ["ClassName", "function_name", ...],
  "outline": "Short structural summary",
  "key_sections": ["section 1", "section 2"]
}
"""
```

---

## Risks & Trade-offs

| Risk | Mitigation |
|------|------------|
| Increased complexity | Start with 4–5 most common tools (Read, Bash, Grep, Write/Edit) |
| Latency increase | Run tool extractors concurrently where possible |
| Schema migration | `fact_source_tool` is nullable — old facts remain valid |
| Over-engineering | Keep fallback generic extractor for unknown tools |

---

## Success Metrics

- `per_tool_extraction_calls` (by tool)
- `per_tool_extraction_failures`
- Improvement in `trace/qa/extract` quality scores
- Reduction in `extraction_empties`
- Operator feedback on fact usefulness

---

## Open Questions

1. Should we run per-tool extraction **in parallel** or sequentially?
2. Do we want to support **custom extraction prompts** per project (via config)?
3. Should `fact_source_tool` be used for filtering in the assembler or recall tool?
4. How do we handle tools that return very large outputs (e.g., full file reads > 200 lines)?

---

## Relationship to Existing Work

- Builds on the recent **Prompt Cache Stability** work (cleaner facts = better cache stability).
- Improves input quality for the **deterministic assembler**.
- Is a prerequisite for several future roadmap items (better fact dedup, smart invalidation, per-tool recall).

---

## Next Steps (if approved)

1. Create this plan as an official document.
2. Implement **Phase 0 + Phase 1** on the review branch.
3. Add basic tests for the new extractors.
4. Run archolith-bench context quality evaluation before/after.

This change has high leverage because it improves the quality of the knowledge graph that everything else (assembler, recall, long-term memory) depends on.