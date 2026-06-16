"""Dependency-edge extraction for the deterministic assembler's topological fill.

Layer 2 of the deterministic-layers direction
(``.agent/plans/archolith-context-deterministic-layers-direction.md``): order the
briefing's files so the load-bearing FOUNDATIONS — files many others depend on —
survive budget truncation. ``scripts/assembly_strategy_sweep.py`` proved a pure
topological sort (most-depended-upon first) protects the recall-critical anchor
better than the Phase-4 scorer, with no LLM and no importance signal. That sweep
used a hand-written dependency map; this module derives the same edges
MECHANICALLY from file contents so it works on an arbitrary corpus.

HONEST CAVEAT (carried from the direction doc): topological quality rests entirely
on this extraction. It is deliberately cheap and corpus-agnostic — it scans for
common reference styles (ES ``import``/``from``, CommonJS ``require``, HTML
``href``/``src``, CSS ``@import``/``url()``, Python ``import``) and matches each
reference against the briefing's OWN file set by basename. It does not resolve
module paths, build an AST, or follow transitive chains beyond direct in-degree.
An unrecognized reference style simply yields no edge (the file keeps its FIFO
position) — it never invents a wrong edge.
"""

from __future__ import annotations

import re

# Quoted-spec references: JS `from '...'` / `import '...'`, CommonJS `require('...')`,
# HTML `href="..."` / `src="..."`, CSS `@import "..."` and `url(...)`.
_QUOTED_REF_PATTERNS = (
    re.compile(r"""\bfrom\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\bimport\s+['"]([^'"]+)['"]"""),
    re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\b(?:href|src)\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    re.compile(r"""@import\s+(?:url\(\s*)?['"]([^'"]+)['"]""", re.IGNORECASE),
    re.compile(r"""\burl\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE),
)
# Python module imports (dotted, unquoted): `import a.b.c`, `from a.b.c import x`.
_PY_IMPORT_PATTERN = re.compile(
    r"""^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)""", re.MULTILINE
)


def _file_text(f) -> str:
    """Concatenate a PreFetchedFile's scannable text (outline + section contents).

    The path is intentionally excluded so a file does not match references to
    itself purely because its own name appears in its path.
    """
    parts = [getattr(f, "outline", "") or ""]
    for section in getattr(f, "sections", []) or []:
        if len(section) >= 3:
            parts.append(section[2] or "")
    return "\n".join(parts)


def _basename(spec: str) -> str:
    """Reduce a reference spec to a bare filename (strip dirs, query, fragment)."""
    spec = spec.split("?", 1)[0].split("#", 1)[0]
    spec = spec.replace("\\", "/").rstrip("/")
    return spec.rsplit("/", 1)[-1]


def _strip_ext(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _candidate_keys(spec: str) -> set[str]:
    """Lookup keys a reference spec could match a target file by."""
    base = _basename(spec)
    keys = {base, _strip_ext(base)}
    # Python dotted module: last segment is the module name.
    if "/" not in spec and "." in spec:
        keys.add(spec.rsplit(".", 1)[-1])
    return {k for k in keys if k}


def _file_keys(path: str) -> set[str]:
    """Keys a target file can be referenced by."""
    base = _basename(path)
    return {k for k in {path, base, _strip_ext(base)} if k}


def _references(text: str) -> set[str]:
    refs: set[str] = set()
    for pat in _QUOTED_REF_PATTERNS:
        refs.update(pat.findall(text))
    refs.update(_PY_IMPORT_PATTERN.findall(text))
    return refs


def extract_dependencies(files) -> dict[str, set[str]]:
    """Map each file path -> set of file paths (in the set) it depends on.

    An edge ``a -> b`` means file ``a``'s contents reference file ``b`` (by
    basename / module name) and both files are in ``files``. Self-edges are
    excluded. References to files outside the set are ignored.
    """
    # Build a lookup from every candidate key to the owning file path. Earlier
    # files win key collisions (stable, deterministic).
    key_to_path: dict[str, str] = {}
    for f in files:
        path = getattr(f, "path", "")
        if not path:
            continue
        for key in _file_keys(path):
            key_to_path.setdefault(key, path)

    deps: dict[str, set[str]] = {getattr(f, "path", ""): set() for f in files}
    for f in files:
        path = getattr(f, "path", "")
        if not path:
            continue
        for spec in _references(_file_text(f)):
            for key in _candidate_keys(spec):
                target = key_to_path.get(key)
                if target and target != path:
                    deps[path].add(target)
                    break  # one edge per reference spec
    return deps


def compute_indegree(files) -> dict[str, int]:
    """In-degree per file = how many OTHER files in the set depend on it.

    Foundations (shared stylesheets, API clients) score highest; leaf pages score 0.
    """
    indeg: dict[str, int] = {getattr(f, "path", ""): 0 for f in files}
    for _src, targets in extract_dependencies(files).items():
        for t in targets:
            if t in indeg:
                indeg[t] += 1
    return indeg


def order_by_topology(files) -> list:
    """Return ``files`` ordered most-depended-upon first (foundations survive truncation).

    Deterministic: sort by in-degree descending, then by path ascending for a
    stable tie-break. Pure function — no LLM, no importance signal. Mirrors
    ``assembly_strategy_sweep.order_topological`` but with extracted edges.
    """
    indeg = compute_indegree(files)
    return sorted(
        files,
        key=lambda f: (-indeg.get(getattr(f, "path", ""), 0), getattr(f, "path", "")),
    )


__all__ = [
    "extract_dependencies",
    "compute_indegree",
    "order_by_topology",
]
