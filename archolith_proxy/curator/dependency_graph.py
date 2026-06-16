"""Dependency-edge extraction for the deterministic assembler's topological fill.

Layer 2 of the deterministic-layers direction
(``.agent/plans/archolith-context-deterministic-layers-direction.md``): order the
briefing's files so the load-bearing FOUNDATIONS — files many others depend on —
survive budget truncation. ``scripts/assembly_strategy_sweep.py`` proved a pure
topological sort (most-depended-upon first) protects the recall-critical anchor
better than the Phase-4 scorer, with no LLM and no importance signal. That sweep
used a hand-written dependency map; this module derives the same edges
MECHANICALLY from file contents so it works on an arbitrary corpus.

Reference resolution (R3a — improved coverage + precision):
- **relative** (``./x``, ``../x``): resolved against the importing file's directory,
  so collisions (several ``types.ts``) edge to the importer's OWN target, not a
  random same-named file.
- **alias / absolute-ish** (``@/x/y``, ``~/x``, ``a/b``): matched as a path SUFFIX
  against the file set (no corpus-specific alias root is hardcoded).
- **bare word / dotted** (``react``, ``a.b.c``): basename / last-segment fallback
  (catches HTML ``href="mobile.css"``, Python ``import a.b.c``).
All forms also try file extensions and ``<dir>/index.*`` so barrel imports
(``from '@/ui'`` -> ``ui/index.ts``) resolve.

HONEST CAVEAT (carried from the direction doc): topological quality still rests on
this extraction. It does not run a real module resolver or read tsconfig path maps;
suffix matching can in principle pick a wrong file when two paths share a tail.
Unknown reference styles yield no edge (the file keeps its FIFO position) — never a
knowingly wrong edge.
"""

from __future__ import annotations

import posixpath
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

# Extensions tried when a reference omits one (and "" for refs that include it).
_EXT_CANDIDATES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".css",
                   ".astro", ".vue", ".svelte", ".json")
_INDEX_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")


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


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def _strip_qf(spec: str) -> str:
    return spec.split("?", 1)[0].split("#", 1)[0].replace("\\", "/").strip()


def _basename(spec: str) -> str:
    return spec.rstrip("/").rsplit("/", 1)[-1]


def _strip_ext(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _candidate_keys(spec: str) -> set[str]:
    """Bare/dotted lookup keys (fallback for package + python + bare-filename refs)."""
    base = _basename(spec)
    keys = {base, _strip_ext(base)}
    if "/" not in spec and "." in spec:  # python dotted module: last segment
        keys.add(spec.rsplit(".", 1)[-1])
    return {k for k in keys if k}


def _file_keys(path: str) -> set[str]:
    base = _basename(path)
    return {k for k in {path, base, _strip_ext(base)} if k}


def _references(text: str) -> set[str]:
    refs: set[str] = set()
    for pat in _QUOTED_REF_PATTERNS:
        refs.update(pat.findall(text))
    refs.update(_PY_IMPORT_PATTERN.findall(text))
    return refs


def _match_base(base: str, path_set: set[str], sorted_paths: list[str]) -> str | None:
    """Match a resolved path stem (already directory-correct) to a file in the set.

    Tries the stem with each extension, then ``<stem>/index.*``. Exact-path first,
    then a deterministic suffix scan for the alias/absolute case.
    """
    # 1. exact path with an extension
    for ext in _EXT_CANDIDATES:
        cand = base + ext
        if cand in path_set:
            return cand
    # 2. directory-index (barrel import)
    for ext in _INDEX_EXTS:
        cand = base + "/index" + ext
        if cand in path_set:
            return cand
    # 3. suffix match (alias/absolute spec whose root differs from our paths)
    for ext in _EXT_CANDIDATES:
        tail = "/" + base + ext
        for p in sorted_paths:
            if p.endswith(tail):
                return p
    for ext in _INDEX_EXTS:
        tail = "/" + base + "/index" + ext
        for p in sorted_paths:
            if p.endswith(tail):
                return p
    return None


def _resolve(spec: str, from_path: str, path_set: set[str],
             sorted_paths: list[str], key_to_path: dict[str, str]) -> str | None:
    spec = _strip_qf(spec)
    if not spec:
        return None

    # relative: resolve against the importing file's directory (precise).
    if spec.startswith("./") or spec.startswith("../"):
        base_dir = posixpath.dirname(from_path)
        resolved = posixpath.normpath(posixpath.join(base_dir, spec))
        return _match_base(resolved, path_set, sorted_paths)

    # alias / home-style: strip the alias token, match the remainder as a suffix.
    if spec.startswith("@/") or spec.startswith("~/"):
        return _match_base(spec[2:], path_set, sorted_paths)

    # other path-ish spec with a separator: try as a path/suffix first.
    if "/" in spec:
        hit = _match_base(spec, path_set, sorted_paths)
        if hit:
            return hit
        # fall through to basename for odd cases

    # bare word / dotted module: basename / last-segment fallback.
    for key in _candidate_keys(spec):
        target = key_to_path.get(key)
        if target:
            return target
    return None


def extract_dependencies(files) -> dict[str, set[str]]:
    """Map each file path -> set of file paths (in the set) it depends on.

    An edge ``a -> b`` means file ``a``'s contents reference file ``b`` and both
    files are in ``files``. Self-edges are excluded; references to files outside
    the set are ignored.
    """
    paths = [_norm(getattr(f, "path", "")) for f in files]
    path_set = {p for p in paths if p}
    sorted_paths = sorted(path_set)

    # Bare/dotted fallback index. Earlier files win key collisions (deterministic).
    key_to_path: dict[str, str] = {}
    for p in paths:
        if not p:
            continue
        for key in _file_keys(p):
            key_to_path.setdefault(key, p)

    deps: dict[str, set[str]] = {p: set() for p in paths}
    for f, fp in zip(files, paths):
        if not fp:
            continue
        for spec in _references(_file_text(f)):
            target = _resolve(spec, fp, path_set, sorted_paths, key_to_path)
            if target and target != fp:
                deps[fp].add(target)
    return deps


def compute_indegree(files) -> dict[str, int]:
    """In-degree per file = how many OTHER files in the set depend on it.

    Foundations (shared stylesheets, API clients) score highest; leaf pages score 0.
    """
    indeg: dict[str, int] = {_norm(getattr(f, "path", "")): 0 for f in files}
    for _src, targets in extract_dependencies(files).items():
        for t in targets:
            if t in indeg:
                indeg[t] += 1
    return indeg


def order_by_combo(files, query: str = "", exemplar_suffixes: tuple[str, ...] = ()) -> list:
    """Exemplar-aware combo fill order (rung-3 Phase D winner).

    Each pure fill optimizes one objective; the combo blends them so a budget-
    truncated briefing keeps all three: a structural EXEMPLAR (the template to
    imitate), task RELEVANCE (scored), and structural FOUNDATIONS (topological).

    Order: if ``exemplar_suffixes`` is given, GUARANTEE the top relevance-scored
    file whose path ends with one of those suffixes (the template) goes first; then
    round-robin interleave the scored ranking and the topological ranking (dedup).
    With no exemplar_suffixes this degenerates to a naive scored/topological
    interleave (which, per Phase D, does NOT beat scored alone — the exemplar
    guarantee is what wins). ``exemplar_suffixes`` is corpus-specific (e.g.
    ``("Page.tsx",)``), the same caveat topological's edge extraction carries.
    """
    from itertools import zip_longest

    from archolith_proxy.curator.scoring import score_files

    scored = [f for _s, f in score_files(files, query)]
    topo = order_by_topology(files)
    out: list = []
    seen: set[str] = set()
    if exemplar_suffixes:
        exemplar = next(
            (f for f in scored
             if _norm(getattr(f, "path", "")).endswith(tuple(exemplar_suffixes))),
            None,
        )
        if exemplar is not None:
            out.append(exemplar)
            seen.add(_norm(getattr(exemplar, "path", "")))
    for a, b in zip_longest(scored, topo):
        for f in (a, b):
            if f is None:
                continue
            p = _norm(getattr(f, "path", ""))
            if p not in seen:
                seen.add(p)
                out.append(f)
    return out


def order_by_topology(files) -> list:
    """Return ``files`` ordered most-depended-upon first (foundations survive truncation).

    Deterministic: sort by in-degree descending, then by path ascending for a
    stable tie-break. Pure function — no LLM, no importance signal. Mirrors
    ``assembly_strategy_sweep.order_topological`` but with extracted edges.
    """
    indeg = compute_indegree(files)
    return sorted(
        files,
        key=lambda f: (-indeg.get(_norm(getattr(f, "path", "")), 0),
                       _norm(getattr(f, "path", ""))),
    )


__all__ = [
    "extract_dependencies",
    "compute_indegree",
    "order_by_topology",
    "order_by_combo",
]
