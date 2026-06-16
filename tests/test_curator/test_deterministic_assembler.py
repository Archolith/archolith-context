"""Tests for the Phase 2 deterministic (LLM-free) inline assembler."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from archolith_proxy.curator.briefing import PreFetchedFile, SessionBriefing  # noqa: E402
from archolith_proxy.curator.deterministic_assembler import (  # noqa: E402
    build_deterministic_context,
    run_deterministic_assembler,
)


def _briefing(**overrides) -> SessionBriefing:
    base = dict(
        session_id="s1",
        source_turn=4,
        session_goal="Build the mobile browse screens",
        checkpoint_text="Added sets.html; next add decks.html",
        open_issues_text="- decksList endpoint missing in api.js",
        last_verification_text="opened cards.html — renders",
        decisions_text="- reuse .list-row for every browse screen",
        facts_text="- tokens are --accent and --muted",
        files=[],
        retained_turns=[2, 3],
    )
    base.update(overrides)
    return SessionBriefing(**base)


def _file(path: str, content: str) -> PreFetchedFile:
    return PreFetchedFile(path=path, outline="", sections=[(1, 10, content)], relevance="r")


# ── build_deterministic_context ─────────────────────────────────────────────


def test_all_small_pools_present_with_ample_budget():
    text, files = build_deterministic_context(_briefing(), token_budget=6000)
    for header in (
        "=== SESSION GOAL ===",
        "=== CURRENT STATE ===",
        "=== OPEN ISSUES ===",
        "=== LAST VERIFICATION ===",
        "=== DECISIONS ===",
        "=== KEY FACTS ===",
    ):
        assert header in text
    assert files == []


def test_sections_in_canonical_order():
    text, _ = build_deterministic_context(_briefing(), token_budget=6000)
    order = [
        text.index("=== SESSION GOAL ==="),
        text.index("=== CURRENT STATE ==="),
        text.index("=== OPEN ISSUES ==="),
        text.index("=== LAST VERIFICATION ==="),
        text.index("=== DECISIONS ==="),
        text.index("=== KEY FACTS ==="),
    ]
    assert order == sorted(order)


def test_relevant_code_included_when_budget_allows():
    b = _briefing(files=[_file("api.js", "export const x = 1;")])
    text, files = build_deterministic_context(b, token_budget=6000)
    assert "=== RELEVANT CODE ===" in text
    assert "api.js lines 1-10:" in text
    assert files == [{"path": "api.js"}]


def test_code_truncated_and_fence_closed_when_over_budget():
    big = "x" * 20000
    b = _briefing(files=[_file("big.js", big)])
    # Tiny budget: small pools fit, code must be truncated.
    text, files = build_deterministic_context(b, token_budget=200)
    assert "[code truncated to fit budget]" in text
    # No dangling open code fence.
    assert text.count("```") % 2 == 0


def test_low_priority_code_dropped_small_pools_kept():
    big = "y" * 20000
    b = _briefing(files=[_file("big.js", big)])
    text, files = build_deterministic_context(b, token_budget=50)
    # The high-value small pools survive even when code cannot fit.
    assert "=== SESSION GOAL ===" in text


def test_outline_used_when_no_sections():
    f = PreFetchedFile(path="m.css", outline="rule A\nrule B", sections=[], relevance="r")
    b = _briefing(files=[f])
    text, files = build_deterministic_context(b, token_budget=6000)
    assert "m.css outline:" in text
    assert files == [{"path": "m.css"}]


def test_empty_briefing_yields_empty_context():
    b = SessionBriefing(session_id="s1", source_turn=1)
    text, files = build_deterministic_context(b, token_budget=6000)
    assert text.strip() == ""
    assert files == []


# ── run_deterministic_assembler ─────────────────────────────────────────────


class _Settings:
    assembler_token_budget = 6000


@pytest.mark.asyncio
async def test_run_returns_assembled_context_without_llm():
    b = _briefing(files=[_file("api.js", "export const x = 1;")])
    result = await run_deterministic_assembler(
        session_id="s1", turn_number=5, user_message="add decks",
        session_goal="g", briefing=b, messages=[],
        client=None, model="unused", settings=_Settings(),
    )
    assert result is not None
    assert result.system_message["role"] == "system"
    assert "=== SESSION GOAL ===" in result.system_message["content"]
    assert result.files_selected == [{"path": "api.js"}]
    assert result.retained_turn_numbers == [2, 3]
    assert result.curator_tool_log == []
    assert result.token_estimate > 0


@pytest.mark.asyncio
async def test_run_returns_none_on_empty_briefing():
    b = SessionBriefing(session_id="s1", source_turn=1)
    result = await run_deterministic_assembler(
        session_id="s1", turn_number=2, user_message="x",
        session_goal=None, briefing=b, messages=[],
        client=None, model="unused", settings=_Settings(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_run_increments_deterministic_assemblies_metric():
    from archolith_proxy.metrics import get_metrics
    before = get_metrics()["deterministic_assemblies"]
    b = _briefing()
    await run_deterministic_assembler(
        session_id="s1", turn_number=5, user_message="x",
        session_goal="g", briefing=b, messages=[],
        client=None, model="unused", settings=_Settings(),
    )
    assert get_metrics()["deterministic_assemblies"] == before + 1


# ── Phase 4: scored file selection ──────────────────────────────────────────


def test_scored_selection_prefers_relevant_file_over_insertion_order():
    big = "X" * 4000  # large bodies so the budget is contended
    files = [
        PreFetchedFile(path="noise.py", outline="",
                       sections=[(1, 10, big)], relevance="score 0.5"),
        PreFetchedFile(path="calculator.py", outline="",
                       sections=[(1, 10, "def multiply(self, x): return " + big)],
                       relevance="score 0.5"),
    ]
    b = _briefing(files=files)
    budget = 1200  # tokens — fits roughly one big block

    fifo_text, fifo_files = build_deterministic_context(b, budget)
    scored_text, scored_files = build_deterministic_context(
        b, budget, scored=True, query="calculator multiply",
    )
    # Insertion order takes the first (irrelevant) file first; scoring takes the
    # query-relevant file first regardless of briefing order.
    assert fifo_files and fifo_files[0]["path"] == "noise.py"
    assert scored_files and scored_files[0]["path"] == "calculator.py"


def test_scored_false_is_identical_to_default():
    files = [_file("a.py", "aaa"), _file("b.py", "bbb")]
    b = _briefing(files=files)
    assert build_deterministic_context(b, 6000) == build_deterministic_context(b, 6000, scored=False)


# ── Layer 2: topological fill ───────────────────────────────────────────────


def test_topological_fill_protects_foundation_under_pressure():
    # A shared stylesheet (the FOUNDATION) is placed LAST and is depended on by
    # several leaf pages placed first. Under a budget that only fits ~one big
    # block, FIFO keeps a leaf and drops the foundation; topological keeps it.
    big = "Z" * 4000
    leaf = '<link rel="stylesheet" href="mobile.css">\n' + big
    files = [
        _file("cards.html", leaf),
        _file("sealed.html", leaf),
        _file("mobile.css", ".list-row{}\n" + big),  # foundation, placed LAST
    ]
    b = _briefing(files=files)
    budget = 1200  # roughly one big block

    _ft, fifo_files = build_deterministic_context(b, budget)
    _tt, topo_files = build_deterministic_context(b, budget, topological=True)

    fifo_paths = [f["path"] for f in fifo_files]
    topo_paths = [f["path"] for f in topo_files]
    # FIFO drops the foundation; topological keeps it first.
    assert "mobile.css" not in fifo_paths
    assert topo_paths and topo_paths[0] == "mobile.css"


def test_topological_takes_precedence_over_scored():
    big = "Q" * 4000
    leaf = "import './api.js';\n" + big
    files = [
        _file("page.html", leaf),                       # query-relevant leaf
        _file("api.js", "export const x = 1;\n" + big),  # foundation, placed last
    ]
    b = _briefing(files=files)
    budget = 1200
    # With both flags on, topological wins -> foundation (api.js) first.
    _t, sel = build_deterministic_context(
        b, budget, scored=True, topological=True, query="page html",
    )
    assert sel and sel[0]["path"] == "api.js"


def test_topological_false_is_identical_to_default():
    files = [_file("a.py", "aaa"), _file("b.py", "bbb")]
    b = _briefing(files=files)
    assert build_deterministic_context(b, 6000) == \
           build_deterministic_context(b, 6000, topological=False)
