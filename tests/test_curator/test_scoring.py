"""Tests for generative-agents scoring helpers (Phase 4)."""

from __future__ import annotations

from archolith_proxy.curator.briefing import PreFetchedFile
from archolith_proxy.curator.scoring import (
    keyword_relevance,
    parse_importance,
    retrieval_score,
    score_files,
)


# -- keyword_relevance ------------------------------------------------------

def test_keyword_relevance_empty_query_is_zero():
    assert keyword_relevance("", "anything here") == 0.0


def test_keyword_relevance_empty_text_is_zero():
    assert keyword_relevance("auth handler", "") == 0.0


def test_keyword_relevance_full_overlap():
    # all content tokens of the query appear in the text
    assert keyword_relevance("calculator multiply", "the Calculator multiply method") == 1.0


def test_keyword_relevance_partial_overlap():
    r = keyword_relevance("calculator subtract divide", "calculator subtract method")
    assert 0.0 < r < 1.0
    assert abs(r - (2 / 3)) < 1e-9


def test_keyword_relevance_ignores_stopwords():
    # query is all stopwords -> no content signal -> 0
    assert keyword_relevance("the a and of", "the a and of file") == 0.0


# -- parse_importance -------------------------------------------------------

def test_parse_importance_default_when_absent():
    assert parse_importance(None) == 0.5
    assert parse_importance("retrieved by curator") == 0.5
    assert parse_importance("") == 0.5


def test_parse_importance_fraction():
    assert parse_importance("score 0.8 | strong match") == 0.8


def test_parse_importance_ten_scale_normalized():
    assert parse_importance("8 / 10 relevance") == 0.8


def test_parse_importance_clamped():
    assert parse_importance("score 99") == 1.0


# -- retrieval_score --------------------------------------------------------

def test_retrieval_score_weighted_sum():
    assert retrieval_score(1.0, 0.5, 0.25) == 1.75
    assert retrieval_score(1.0, 0.5, 0.25, weights=(0.0, 2.0, 4.0)) == 2.0


# -- score_files ------------------------------------------------------------

def _f(path, relevance="", outline="", sections=None):
    return PreFetchedFile(path=path, outline=outline, sections=sections or [], relevance=relevance)


def test_score_files_ranks_relevant_first():
    files = [
        _f("unrelated.py", relevance="score 0.5", outline="def helper"),
        _f("calculator.py", relevance="score 0.5", outline="class Calculator: def multiply"),
    ]
    ranked = score_files(files, query="calculator multiply")
    assert ranked[0][1].path == "calculator.py"


def test_score_files_importance_breaks_when_relevance_equal():
    files = [
        _f("a.py", relevance="score 0.2"),
        _f("b.py", relevance="score 0.9"),
    ]
    ranked = score_files(files, query="nothing matches here xyz")
    assert ranked[0][1].path == "b.py"  # higher importance wins on equal (zero) relevance


def test_score_files_stable_on_ties():
    files = [_f("a.py"), _f("b.py"), _f("c.py")]
    ranked = score_files(files, query="zzz no overlap")
    # all equal score -> original order preserved
    assert [f.path for (_s, f) in ranked] == ["a.py", "b.py", "c.py"]


def test_score_files_searches_section_content():
    files = [
        _f("a.py", outline="def f", sections=[(1, 5, "irrelevant body")]),
        _f("b.py", outline="def g", sections=[(1, 5, "def multiply(self, x): return x")]),
    ]
    ranked = score_files(files, query="multiply")
    assert ranked[0][1].path == "b.py"
