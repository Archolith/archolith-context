"""Tests for the deterministic corpus profiler (derives combo role markers)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from archolith_proxy.curator.briefing import PreFetchedFile  # noqa: E402
from archolith_proxy.curator.corpus_profile import (  # noqa: E402
    derive_corpus_profile,
    _component_pattern,
)


def _f(path: str, content: str = "x") -> PreFetchedFile:
    return PreFetchedFile(path=path, outline="",
                          sections=[(1, content.count("\n") + 1, content)], relevance="r")


def test_component_pattern_extracts_trailing_word_plus_ext():
    assert _component_pattern("CardsV3Page.tsx") == "Page.tsx"
    assert _component_pattern("useCardsV3Data.ts") == "Data.ts"
    assert _component_pattern("CardTile.tsx") == "Tile.tsx"
    assert _component_pattern("DecksPage.module.css") == "Page.module.css"
    assert _component_pattern("index.ts") is None       # no PascalCase tail
    assert _component_pattern("slug.ts") is None


def test_derives_recurring_page_marker_across_sibling_dirs():
    files = [
        _f("features/cards/CardsPage.tsx", "export default function P(){}"),
        _f("features/cards/useCardsData.ts"),
        _f("features/sets/SetsPage.tsx", "export default function P(){}"),
        _f("features/sets/useSetsData.ts"),
        _f("features/sealed/SealedPage.tsx", "export default function P(){}"),
        _f("features/sealed/useSealedData.ts"),
        _f("data/apiClient.ts"),
    ]
    prof = derive_corpus_profile(files, min_recurrence=3)
    # 'Page.tsx' recurs in 3 sibling feature dirs -> the derived exemplar marker.
    assert prof.exemplar_markers == ["Page.tsx"]
    assert prof.exemplar_suffixes() == ("Page.tsx",)
    # 'Data.ts' also recurs but is a hook (.ts), correctly NOT an exemplar (component) marker.
    pats = dict(prof.recurring_patterns)
    assert pats.get("Page.tsx") == 3
    assert pats.get("Data.ts") == 3
    assert "Data.ts" not in prof.exemplar_markers


def test_foundations_are_top_indegree():
    files = [
        _f("data/api.ts", "export const api=1;"),
        _f("features/a/APage.tsx", "import {api} from '@/data/api';"),
        _f("features/b/BPage.tsx", "import {api} from '@/data/api';"),
    ]
    prof = derive_corpus_profile(files, min_recurrence=2)
    assert "data/api.ts" in prof.foundation_files


def test_below_recurrence_threshold_yields_no_exemplar():
    files = [
        _f("features/only/OnlyPage.tsx", "export default function P(){}"),
        _f("data/api.ts"),
    ]
    # Page.tsx appears in only 1 dir -> below default min_recurrence (3) -> no marker.
    assert derive_corpus_profile(files).exemplar_markers == []
