"""Tests for bounded session reconciliation cache."""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from archolith_proxy.metrics import get_metrics
from archolith_proxy.proxy import session as session_mod


@pytest.fixture(autouse=True)
def _reset_reconciled_sessions():
    session_mod._reset_sessions()
    old_metric = get_metrics()["reconciled_set_size"]
    get_metrics()["reconciled_set_size"] = 0
    yield
    session_mod._reset_sessions()
    get_metrics()["reconciled_set_size"] = old_metric


@pytest.mark.asyncio
async def test_reconciled_sessions_cache_is_bounded_lru(monkeypatch) -> None:
    async def _no_trace(_session_id: str):
        return None

    monkeypatch.setattr(session_mod, "_RECONCILED_SESSIONS_MAX", 3)
    monkeypatch.setattr(session_mod, "get_trace_store", lambda: SimpleNamespace(get_max_turn_number=_no_trace))

    for session_id in ("s1", "s2", "s3", "s4"):
        await session_mod._reconcile_turn_number(session_id)

    assert isinstance(session_mod._reconciled_sessions, OrderedDict)
    assert list(session_mod._reconciled_sessions) == ["s2", "s3", "s4"]
    assert get_metrics()["reconciled_set_size"] == 3


@pytest.mark.asyncio
async def test_reconciled_sessions_cache_refreshes_recency(monkeypatch) -> None:
    async def _no_trace(_session_id: str):
        return None

    monkeypatch.setattr(session_mod, "_RECONCILED_SESSIONS_MAX", 3)
    monkeypatch.setattr(session_mod, "get_trace_store", lambda: SimpleNamespace(get_max_turn_number=_no_trace))

    for session_id in ("s1", "s2", "s3"):
        await session_mod._reconcile_turn_number(session_id)

    await session_mod._reconcile_turn_number("s1")
    await session_mod._reconcile_turn_number("s4")

    assert list(session_mod._reconciled_sessions) == ["s3", "s1", "s4"]
