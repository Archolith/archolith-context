"""Generative-agents retrieval scoring for context working-set selection (Phase 4).

From Park et al. "Generative Agents" — a memory's retrieval score is a weighted
sum of normalized *recency*, *importance*, and *relevance*. Here we score the
prepper's pre-fetched files so the deterministic assembler keeps the highest-value
files when the token budget can't hold them all (replacing naive insertion order).

Signals available without an LLM or embeddings on the hot path:
- recency:   intra-briefing files are fetched in one prepper pass, so recency is
             ~uniform and contributes a constant. The parameter is kept so the
             same function serves a future ledger whose entries carry timestamps.
- importance: parsed from the prepper's ``score_file_relevance`` reason string
             (e.g. "score 0.8 | ..."); default 0.5 when no number is present.
- relevance: cheap token-overlap between the current user message and the file's
             path + outline + fetched section text. No embeddings on the hot path.
"""

from __future__ import annotations

import re

# Tokenizer for keyword relevance — words of length >= 2, lowercased.
_WORD_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]+")
# A leading float in a relevance/reason string, e.g. "score 0.82 | ...", "0.4".
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

# Generic English/code stopwords that would inflate overlap noise.
_STOP = frozenset(
    "the a an and or of to in is it for on with this that be as at by from "
    "do does add now then your you we can class method function file code line".split()
)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOP}


def keyword_relevance(query: str, text: str) -> float:
    """Normalized token overlap of ``query`` against ``text``. Returns 0..1.

    Fraction of the query's distinct content tokens that appear in ``text``.
    Empty query or text -> 0.0 (no signal).
    """
    q = _tokens(query)
    if not q:
        return 0.0
    t = _tokens(text)
    if not t:
        return 0.0
    return len(q & t) / len(q)


def parse_importance(relevance_str: str | None) -> float:
    """Extract a 0..1 importance from the prepper's reason string.

    The prepper's ``score_file_relevance`` writes reasons that often start with a
    numeric score. If a number is found, clamp it to 0..1 (values > 1 are treated
    as a 0..10 scale and divided by 10). No number -> neutral default 0.5.
    """
    if not relevance_str:
        return 0.5
    m = _NUM_RE.search(relevance_str)
    if not m:
        return 0.5
    try:
        val = float(m.group(1))
    except ValueError:
        return 0.5
    if val > 1.0:
        val = val / 10.0
    return max(0.0, min(1.0, val))


def retrieval_score(
    recency: float,
    importance: float,
    relevance: float,
    weights: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> float:
    """Generative-agents weighted sum of normalized components (each ~0..1)."""
    wr, wi, wrel = weights
    return wr * recency + wi * importance + wrel * relevance


def _file_text(f) -> str:
    """Concatenate a PreFetchedFile's searchable text (path + outline + sections)."""
    parts = [getattr(f, "path", ""), getattr(f, "outline", "") or ""]
    for section in getattr(f, "sections", []) or []:
        # section is (start, end, content)
        if len(section) >= 3:
            parts.append(section[2] or "")
    return "\n".join(parts)


def score_files(
    files,
    query: str,
    weights: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> list[tuple[float, object]]:
    """Score and rank PreFetchedFiles by retrieval score, highest first.

    recency is uniform across one briefing (all fetched together), so ranking is
    driven by importance (parsed prepper score) + relevance (turn keyword overlap).
    Stable: ties preserve the original (prepper-provided) order.
    """
    scored: list[tuple[float, int, object]] = []
    for idx, f in enumerate(files):
        importance = parse_importance(getattr(f, "relevance", ""))
        relevance = keyword_relevance(query, _file_text(f))
        score = retrieval_score(1.0, importance, relevance, weights)
        scored.append((score, idx, f))
    # Sort by score desc, then original index asc (stable tie-break).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(score, f) for (score, _idx, f) in scored]


__all__ = [
    "keyword_relevance",
    "parse_importance",
    "retrieval_score",
    "score_files",
]
