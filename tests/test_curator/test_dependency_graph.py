"""Tests for the Layer-2 dependency-edge extractor (topological fill)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from archolith_proxy.curator.briefing import PreFetchedFile  # noqa: E402
from archolith_proxy.curator.dependency_graph import (  # noqa: E402
    compute_indegree,
    extract_dependencies,
    order_by_topology,
)


def _f(path: str, content: str) -> PreFetchedFile:
    return PreFetchedFile(path=path, outline="",
                          sections=[(1, content.count("\n") + 1, content)], relevance="r")


# ── extract_dependencies ────────────────────────────────────────────────────


def test_html_link_and_import_edges():
    files = [
        _f("mobile.css", ".list-row{}"),
        _f("api.js", "export const sealedList = () => {};"),
        _f("cards.html",
           '<link rel="stylesheet" href="mobile.css">'
           "<script type=module>import { cardSearch } from './api.js';</script>"),
    ]
    deps = extract_dependencies(files)
    assert deps["cards.html"] == {"mobile.css", "api.js"}
    assert deps["mobile.css"] == set()
    assert deps["api.js"] == set()


def test_require_and_css_url_edges():
    files = [
        _f("util.js", "module.exports = {};"),
        _f("theme.css", "@import 'base.css';"),
        _f("base.css", ":root{}"),
        _f("app.js", "const u = require('./util.js');"),
    ]
    deps = extract_dependencies(files)
    assert deps["app.js"] == {"util.js"}
    assert deps["theme.css"] == {"base.css"}


def test_python_dotted_import_edge():
    files = [
        _f("briefing.py", "class SessionBriefing: ..."),
        _f("assembler.py", "from archolith_proxy.curator.briefing import SessionBriefing"),
    ]
    deps = extract_dependencies(files)
    assert deps["assembler.py"] == {"briefing.py"}


def test_self_reference_excluded():
    # A nav link to the page's own filename must not create a self-edge.
    files = [_f("cards.html", '<a href="cards.html">Cards</a>')]
    assert extract_dependencies(files)["cards.html"] == set()


def test_reference_outside_set_ignored():
    files = [_f("a.js", "import x from './nonexistent.js';")]
    assert extract_dependencies(files)["a.js"] == set()


def test_query_string_and_fragment_stripped():
    files = [
        _f("card-detail.html", "<h1>detail</h1>"),
        _f("cards.html", '<a href="card-detail.html?id=5#top">go</a>'),
    ]
    assert extract_dependencies(files)["cards.html"] == {"card-detail.html"}


# ── compute_indegree / order_by_topology ────────────────────────────────────


def test_indegree_ranks_foundation_highest():
    files = [
        _f("mobile.css", ".x{}"),
        _f("api.js", "export const a = 1;"),
        _f("cards.html",
           '<link href="mobile.css">import "./api.js";'),
        _f("sealed.html",
           '<link href="mobile.css">import "./api.js";'),
        _f("market.html", '<link href="mobile.css">'),
    ]
    indeg = compute_indegree(files)
    assert indeg["mobile.css"] == 3      # depended on by 3 pages
    assert indeg["api.js"] == 2          # depended on by 2 pages
    assert indeg["cards.html"] == 0      # a leaf


def test_order_puts_foundations_first_then_path_tiebreak():
    files = [
        _f("zebra.html", '<link href="mobile.css">'),
        _f("alpha.html", '<link href="mobile.css">'),
        _f("mobile.css", ".x{}"),
    ]
    ordered = [f.path for f in order_by_topology(files)]
    # mobile.css (in-degree 2) first; the two leaves (in-degree 0) by path.
    assert ordered == ["mobile.css", "alpha.html", "zebra.html"]


def test_order_is_stable_and_deterministic():
    files = [
        _f("a.html", '<link href="base.css">'),
        _f("b.html", '<link href="base.css">'),
        _f("base.css", ".x{}"),
    ]
    assert [f.path for f in order_by_topology(files)] == \
           [f.path for f in order_by_topology(files)]
