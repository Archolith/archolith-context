"""Corpus profiling — derive the combo fill's role markers from the corpus itself.

The exemplar-aware combo fill (rung-3 Phase D winner) needs to know which files are
structural EXEMPLARS (the template the model imitates) and which are FOUNDATIONS.
`assembler_exemplar_suffixes` currently hardcodes that per corpus (`Page.tsx`).
This derives it instead, with NO LLM and NO hardcoding, from the corpus's OWN
repetition: a convention is a filename pattern a codebase repeats across many
sibling feature directories. `Page.tsx` recurs in ~every feature dir -> it is the
template marker; the corpus declares its conventions by repeating them.

Pure, deterministic, off-hot-path. Output is a small `CorpusProfile` meant to be
computed once and cached, then consumed by `order_by_combo`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from archolith_proxy.curator.dependency_graph import compute_indegree

_PASCAL = re.compile(r"[A-Z][a-z0-9]*")
_COMPONENT_EXTS = {".tsx", ".jsx", ".vue", ".svelte"}


@dataclass
class CorpusProfile:
    exemplar_markers: list[str] = field(default_factory=list)   # e.g. ["Page.tsx"]
    foundation_files: list[str] = field(default_factory=list)   # top in-degree paths
    recurring_patterns: list[tuple[str, int]] = field(default_factory=list)  # (pattern, #dirs)

    def exemplar_suffixes(self) -> tuple[str, ...]:
        return tuple(self.exemplar_markers)


def _stem_ext(basename: str) -> tuple[str, str]:
    """Split a filename into (stem, compound-ext). 'X.module.css' -> ('X', '.module.css')."""
    parts = basename.split(".")
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], "." + ".".join(parts[1:])


def _component_pattern(basename: str) -> str | None:
    """Trailing PascalCase word + extension, e.g. 'CardsV3Page.tsx' -> 'Page.tsx'.

    Returns None for files with no PascalCase tail (index.ts, util.ts) — those are
    not template-shaped, so they are not exemplar candidates.
    """
    stem, ext = _stem_ext(basename)
    if not ext:
        return None
    words = _PASCAL.findall(stem)
    if not words:
        return None
    return words[-1] + ext


def derive_corpus_profile(
    files,
    *,
    top_exemplars: int = 1,
    top_foundations: int = 8,
    min_recurrence: int = 3,
) -> CorpusProfile:
    """Derive role markers from a file set (the whole corpus, ideally).

    - exemplar markers: component-file (`.tsx`/`.jsx`/...) trailing-word+ext patterns
      that recur across the most distinct parent directories (>= ``min_recurrence``).
    - foundation files: highest dependency in-degree (most depended-upon).
    """
    # pattern -> set of distinct parent dirs it appears in (recurrence across siblings)
    pattern_dirs: dict[str, set[str]] = {}
    for f in files:
        path = getattr(f, "path", "").replace("\\", "/")
        if not path:
            continue
        base = posixpath.basename(path)
        parent = posixpath.dirname(path)
        pat = _component_pattern(base)
        if pat is None:
            continue
        pattern_dirs.setdefault(pat, set()).add(parent)

    ranked = sorted(
        ((pat, len(dirs)) for pat, dirs in pattern_dirs.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    recurring = [(p, n) for p, n in ranked if n >= min_recurrence]

    # exemplar markers = top recurring COMPONENT patterns (a template is a component)
    exemplars = [p for p, _n in recurring if _stem_ext(p)[1] in _COMPONENT_EXTS][:top_exemplars]

    indeg = compute_indegree(files)
    foundations = [
        p for p, _v in sorted(indeg.items(), key=lambda kv: (-kv[1], kv[0]))
        if _v > 0
    ][:top_foundations]

    return CorpusProfile(
        exemplar_markers=exemplars,
        foundation_files=foundations,
        recurring_patterns=recurring,
    )


__all__ = ["CorpusProfile", "derive_corpus_profile"]
