"""Head-to-head: which assembly strategy protects the convention anchor under budget pressure?

Phase 5 follow-up #2. Tests whether a DETERMINISTIC structural strategy (topological order /
dependency-propagated relevance) protects the recall-critical files as well as, or better than, the
generative-agents SCORED strategy — and far better than FIFO. If a pure sort wins, the elegant answer
was simpler than the Phase-4 machinery (and needs no LLM importance signal at all).

Strategies compared, all filling the same assembler budget, convention files placed LAST (worst case):
  fifo        - briefing insertion order (current default when scored=off)
  scored      - generative-agents score (importance + keyword relevance), Phase 4
  propagated  - scored + one-hop dependency propagation (anchor inherits dependents' scores)  [Axis A]
  topological - DETERMINISTIC: depended-upon files first (foundations survive truncation)      [#3]

No server, no LLM, no cost.
"""

from __future__ import annotations

from pathlib import Path

from archolith_proxy.curator.briefing import PreFetchedFile, SessionBriefing
from archolith_proxy.curator.deterministic_assembler import build_deterministic_context, _format_file_block
from archolith_proxy.curator.scoring import score_files

_SEED = Path(
    r"C:\Users\thron\IdeaProjects\projects\archolith\archolith-bench"
    r"\experiments\context-quality\seeded\_seed"
)
QUERY = (
    "Now add the Sealed products browse screen, consistent with the rest of the app. "
    "It lists sealed products, each showing its expected value (EV)."
)
CONV = ["mobile.css", "api.js", "cards.html", "card-detail.html"]

# Dependency graph for the real corpus (who depends on whom).
#   every .html <link>s mobile.css and uses its classes  -> depends on mobile.css
#   data-fetching pages import api.js                     -> depend on api.js
#   distractors depend on nothing relevant
# Foundations (high in-degree) = mobile.css, then api.js. Pages are leaves.
DEPENDS_ON = {
    "cards.html": {"mobile.css", "api.js"},
    "card-detail.html": {"mobile.css", "api.js"},
    # distractors + the anchors themselves added at build time with no deps
}


def _load_seed(name: str, importance: float) -> PreFetchedFile:
    content = (_SEED / name).read_text(encoding="utf-8")
    return PreFetchedFile(path=name, outline="",
                          sections=[(1, content.count("\n") + 1, content)],
                          relevance=f"score {importance:.2f}")


def _distractor(i: int) -> PreFetchedFile:
    body = ("export function widget%d(){\n" % i
            + "  // analytics panel, telemetry, settings, flags\n" * 12 + "}\n")
    return PreFetchedFile(path=f"component_{i}.js", outline="",
                          sections=[(1, body.count("\n") + 1, body)], relevance="score 0.50")


def build_files(n_distractors: int, conv_importance: float) -> list[PreFetchedFile]:
    files = [_distractor(i) for i in range(n_distractors)]
    files += [_load_seed(n, conv_importance) for n in CONV]  # convention files LAST
    return files


# --- strategies: each returns the files in fill order ---

def order_fifo(files):
    return list(files)


def order_scored(files):
    return [f for _s, f in score_files(files, QUERY)]


def order_propagated(files):
    base = {f.path: s for s, f in score_files(files, QUERY)}
    boosted = dict(base)
    for f in files:
        for dep in DEPENDS_ON.get(f.path, ()):  # f depends on dep -> dep inherits f's score
            if dep in boosted:
                boosted[dep] += base.get(f.path, 0.0)
    return sorted(files, key=lambda f: boosted.get(f.path, 0.0), reverse=True)


def order_topological(files):
    # Deterministic: depended-upon-count (in-degree) desc, then path. Foundations first.
    indeg = {f.path: 0 for f in files}
    for f in files:
        for dep in DEPENDS_ON.get(f.path, ()):
            if dep in indeg:
                indeg[dep] += 1
    return sorted(files, key=lambda f: (-indeg.get(f.path, 0), f.path))


STRATEGIES = {
    "fifo": order_fifo,
    "scored": order_scored,
    "propagated": order_propagated,
    "topological": order_topological,
}


def survivors(files, budget) -> set[str]:
    # Reuse the real assembler fill logic by handing it a briefing whose file order
    # is the strategy's order (scored=False so it fills in that given order).
    br = SessionBriefing(session_id="s", source_turn=5, session_goal="g", files=files)
    _text, selected = build_deterministic_context(br, budget, scored=False, query=QUERY)
    return {f["path"] for f in selected}


def conv_kept(surv) -> str:
    kept = [c.split(".")[0] for c in CONV if c in surv]
    return f"{len(kept)}/4 [{','.join(kept)}]"


def run():
    print("Convention files placed LAST (worst case for FIFO). Importance UNIFORM 0.5 (no prepper hint).")
    print(f"{'budget':>7} | " + " | ".join(f"{name:>22}" for name in STRATEGIES))
    files = build_files(14, conv_importance=0.5)
    for budget in (6000, 4000, 3000, 2000, 1500, 1000, 700, 500):
        cells = []
        for name, fn in STRATEGIES.items():
            ordered = fn(files)
            cells.append(f"{conv_kept(survivors(ordered, budget)):>22}")
        print(f"{budget:>7} | " + " | ".join(cells))


if __name__ == "__main__":
    run()
