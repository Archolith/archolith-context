"""Tests for D2 — file-cache recall distinguishes ambiguous from not-cached.

get_file_content previously returned None on an ambiguous suffix match (two
stored paths of equal length), indistinguishable from a genuine cache miss, so
assembly silently dropped the file context. It now resolves deterministically
to the lexicographically-smallest match and warns.
"""

from __future__ import annotations

import pytest

from archolith_proxy.graph.ladybug_files import get_file_content


def _make_execute(exact_rows, suffix_rows):
    """Fake `execute`: returns exact_rows for the exact-path query, suffix_rows
    for the ENDS WITH query."""
    async def _execute(cypher, params=None):
        return suffix_rows if "ENDS WITH" in cypher else exact_rows
    return _execute


@pytest.mark.asyncio
async def test_exact_match_short_circuits():
    exact = [{"content": "C", "sha256": "h", "line_count": 1}]
    result = await get_file_content(_make_execute(exact, []), "s1", "src/app.py")
    assert result["content"] == "C"


@pytest.mark.asyncio
async def test_single_suffix_match_returned():
    suffix = [{"content": "C", "sha256": "h", "line_count": 1,
               "stored_path": "a/b/app.py", "path_len": 9}]
    result = await get_file_content(_make_execute([], suffix), "s1", "app.py")
    assert result["content"] == "C"
    assert result["stored_path"] == "a/b/app.py"


@pytest.mark.asyncio
async def test_genuine_miss_returns_none():
    result = await get_file_content(_make_execute([], []), "s1", "nope.py")
    assert result is None


@pytest.mark.asyncio
async def test_ambiguous_resolves_deterministically_not_none():
    # Two equal-length matches: must return the first (not None) and warn.
    suffix = [
        {"content": "A", "sha256": "h1", "line_count": 1, "stored_path": "x/app.py", "path_len": 8},
        {"content": "B", "sha256": "h2", "line_count": 2, "stored_path": "y/app.py", "path_len": 8},
    ]
    result = await get_file_content(_make_execute([], suffix), "s1", "app.py")
    assert result is not None
    assert result["content"] == "A"
    assert result["stored_path"] == "x/app.py"
