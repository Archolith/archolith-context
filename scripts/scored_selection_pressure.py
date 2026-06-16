"""Offline file-budget pressure sweep for scored vs FIFO assembler selection.

Phase 5 follow-up: the live seeded A/B never created budget pressure (8 small
files fit the assembler budget), so scored selection (Phase 4) was never
exercised. This script walks the budget DOWN against a realistic many-file
briefing and reports, per regime, whether the convention-defining files survive
under FIFO insertion order vs generative-agents scored order — i.e. where recall
would crack, and whether scoring protects the critical files.

It uses the REAL seeded files (mobile.css/api.js/cards.html/card-detail.html) as
the convention set, plus synthetic distractor files the prepper also fetched.
The briefing places the convention files LAST (worst case for FIFO).

No server, no LLM, no cost — pure call into build_deterministic_context.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Real seed files live in the bench repo.
_SEED = Path(
    r"C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench"
    r"\experiments\context-quality\seeded\_seed"
)

from archolith_proxy.curator.briefing import PreFetchedFile, SessionBriefing
from archolith_proxy.curator.deterministic_assembler import build_deterministic_context
from archolith_proxy.curator.scoring import score_files, parse_importance, keyword_relevance

# The under-specified page intent the agent is asked on the comparison turn.
QUERY = (
    "Now add the Sealed products browse screen, consistent with the rest of the app. "
    "It lists sealed products, each showing its expected value (EV)."
)

# Convention-defining files (recall-critical): reuse establishes .list-row /
# --accent / .row-meta / named api helper / detail header.
CONV_FILES = ["mobile.css", "api.js", "cards.html", "card-detail.html"]


def _load_seed(name: str, importance: float) -> PreFetchedFile:
    content = (_SEED / name).read_text(encoding="utf-8")
    return PreFetchedFile(
        path=name,
        outline="",
        sections=[(1, content.count("\n") + 1, content)],
        relevance=f"score {importance:.2f}",
    )


def _distractor(i: int, importance: float) -> PreFetchedFile:
    # ~400-token generic page the prepper also fetched; carries none of the
    # conventions and shares little vocabulary with the query.
    body = (
        f"<!-- component_{i}.js: unrelated widget -->\n"
        + "export function widget%d(opts) {\n  const node = document.createElement('div');\n" % i
        + "  node.dataset.kind = 'analytics-panel-%d';\n" % i
        + "  // chart rendering, telemetry, settings persistence, feature flags\n" * 12
        + "  return node;\n}\n"
    )
    return PreFetchedFile(
        path=f"component_{i}.js",
        outline="",
        sections=[(1, body.count("\n") + 1, body)],
        relevance=f"score {importance:.2f}",
    )


def build_briefing(n_distractors: int, conv_importance: float, distr_importance: float) -> SessionBriefing:
    # Distractors FIRST, convention files LAST = worst case for FIFO.
    files = [_distractor(i, distr_importance) for i in range(n_distractors)]
    files += [_load_seed(n, conv_importance) for n in CONV_FILES]
    return SessionBriefing(session_id="pressure", source_turn=5, session_goal="g", files=files)


def survivors(briefing: SessionBriefing, budget: int, scored: bool) -> set[str]:
    _text, selected = build_deterministic_context(
        briefing, budget, scored=scored, query=QUERY,
    )
    return {f["path"] for f in selected}


def conv_survived(surv: set[str]) -> str:
    kept = [c for c in CONV_FILES if c in surv]
    return f"{len(kept)}/4 [{','.join(c.split('.')[0] for c in kept)}]"


def run():
    print("QUERY:", QUERY[:70], "...\n")

    # --- Diagnostic: how does scoring rank the convention files vs distractors? ---
    print("=== Per-file score (regime: prepper UNIFORM importance 0.5) ===")
    b = build_briefing(6, conv_importance=0.5, distr_importance=0.5)
    ranked = score_files(b.files, QUERY)
    for score, f in ranked:
        imp = parse_importance(f.relevance)
        rel = keyword_relevance(QUERY, "\n".join(s[2] for s in f.sections) + " " + f.path)
        tag = "CONV" if f.path in CONV_FILES else "dist"
        print(f"  {score:5.2f}  imp={imp:.2f} rel={rel:.2f}  {tag}  {f.path}")

    # --- Sweep: budget down, two importance regimes ---
    for regime, conv_imp, distr_imp in [
        ("UNIFORM importance (prepper did not differentiate)", 0.5, 0.5),
        ("PREPPER-FAVORED (conv files scored 0.85, distractors 0.5)", 0.85, 0.5),
    ]:
        print(f"\n=== Regime: {regime} ===")
        print(f"  {'budget':>7} {'#files':>6} | {'FIFO conv kept':>22} | {'SCORED conv kept':>22}")
        n_distr = 14  # enough to force eviction at small budgets
        for budget in (6000, 4000, 3000, 2000, 1500, 1000, 700, 500):
            br = build_briefing(n_distr, conv_imp, distr_imp)
            fifo = survivors(br, budget, scored=False)
            scd = survivors(br, budget, scored=True)
            print(f"  {budget:>7} {len(br.files):>6} | {conv_survived(fifo):>22} | {conv_survived(scd):>22}")


if __name__ == "__main__":
    run()
