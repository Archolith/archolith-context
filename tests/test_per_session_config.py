"""Per-session config overrides — overlay core, request helper, and propagation.

Covers:
- build_effective_settings precedence / denylist / unknown-field / type coercion
- get_settings() returning the session overlay when active
- contextvar propagation into an asyncio.create_task (the mechanism the curator
  background pass relies on)
- _apply_session_config_overlay merge/persist/activate against a fake backend and
  a real LadybugBackend
"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from archolith_proxy.config import (
    SESSION_CONFIG_DENYLIST,
    build_effective_settings,
    get_settings,
    reset_session_settings,
    set_session_settings,
)


# ── Overlay core (config.py) ─────────────────────────────────────────────


def test_build_effective_settings_applies_overrides():
    base = get_settings()
    eff = build_effective_settings(
        {"embedding_enabled": not base.embedding_enabled, "context_token_budget": 999}
    )
    assert eff.embedding_enabled == (not base.embedding_enabled)
    assert eff.context_token_budget == 999
    # Base singleton must be untouched (model_copy isolation).
    assert get_settings().context_token_budget == base.context_token_budget


def test_build_effective_settings_denylist_blocks_secrets():
    assert "upstream_api_key" in SESSION_CONFIG_DENYLIST
    base = get_settings()
    eff = build_effective_settings({"upstream_api_key": "EVIL", "context_token_budget": 123})
    assert eff.upstream_api_key == base.upstream_api_key  # blocked
    assert eff.context_token_budget == 123  # non-denylisted still applies


def test_build_effective_settings_denylist_blocks_security_toggles():
    assert "curator_enabled" in SESSION_CONFIG_DENYLIST
    assert "filter_enabled" in SESSION_CONFIG_DENYLIST
    assert "synthetic_tools_enabled" in SESSION_CONFIG_DENYLIST
    assert "native_read_intercept_enabled" in SESSION_CONFIG_DENYLIST
    assert "drop_middle_on_assembly" in SESSION_CONFIG_DENYLIST
    base = get_settings()
    eff = build_effective_settings({
        "curator_enabled": not base.curator_enabled,
        "filter_enabled": not base.filter_enabled,
        "synthetic_tools_enabled": not base.synthetic_tools_enabled,
        "native_read_intercept_enabled": not base.native_read_intercept_enabled,
        "drop_middle_on_assembly": not base.drop_middle_on_assembly,
    })
    assert eff.curator_enabled == base.curator_enabled
    assert eff.filter_enabled == base.filter_enabled
    assert eff.synthetic_tools_enabled == base.synthetic_tools_enabled
    assert eff.native_read_intercept_enabled == base.native_read_intercept_enabled
    assert eff.drop_middle_on_assembly == base.drop_middle_on_assembly


def test_build_effective_settings_ignores_unknown_field():
    eff = build_effective_settings({"definitely_not_a_setting": 1, "context_token_budget": 321})
    assert not hasattr(eff, "definitely_not_a_setting")
    assert eff.context_token_budget == 321


def test_build_effective_settings_coerces_type():
    eff = build_effective_settings({"context_token_budget": "777"})
    assert eff.context_token_budget == 777
    assert isinstance(eff.context_token_budget, int)


def test_get_settings_returns_overlay_then_restores():
    base_budget = get_settings().context_token_budget
    eff = build_effective_settings({"context_token_budget": 4242})
    token = set_session_settings(eff)
    try:
        assert get_settings().context_token_budget == 4242
    finally:
        reset_session_settings(token)
    assert get_settings().context_token_budget == base_budget


# ── Contextvar propagation (the curator background-pass mechanism) ────────


@pytest.mark.asyncio
async def test_overlay_propagates_to_created_task():
    """A task spawned while the overlay is active keeps it even after the
    spawner resets — this is how the create_task curator background pass inherits
    the session config."""
    eff = build_effective_settings({"context_token_budget": 5555})
    seen = {}

    async def worker():
        await asyncio.sleep(0.02)
        seen["budget"] = get_settings().context_token_budget

    token = set_session_settings(eff)
    task = asyncio.create_task(worker())  # copies the current (overlay) context
    reset_session_settings(token)  # spawner restores immediately
    # Spawner no longer sees the overlay...
    assert get_settings().context_token_budget != 5555
    await task
    # ...but the already-spawned task still does.
    assert seen["budget"] == 5555


# ── _apply_session_config_overlay (request helper) ───────────────────────


def _fake_backend(stored: str = ""):
    """Stateful fake: set persists, get returns the last persisted value.

    Mirrors the helper's read -> merge -> write -> read-back sequence.
    """
    state = {"value": stored}
    be = AsyncMock()

    async def _get(_session_id):
        return state["value"]

    async def _set(_session_id, overrides_json):
        state["value"] = overrides_json

    be.get_session_config_overrides = AsyncMock(side_effect=_get)
    be.set_session_config_overrides = AsyncMock(side_effect=_set)
    return be


@pytest.mark.asyncio
async def test_header_merges_persists_and_activates():
    from archolith_proxy.openai.chat import _apply_session_config_overlay

    be = _fake_backend(stored="")
    with patch("archolith_proxy.openai.chat.get_backend", return_value=be):
        header = json.dumps({"context_token_budget": 888})
        eff = await _apply_session_config_overlay(header, "sess-1", get_settings())

    # Persisted the merged overrides containing the applied field.
    be.set_session_config_overrides.assert_awaited_once()
    persisted = json.loads(be.set_session_config_overrides.await_args.args[1])
    assert persisted == {"context_token_budget": 888}
    # Overlay activated and returned.
    assert eff.context_token_budget == 888
    assert get_settings().context_token_budget == 888
    set_session_settings(None)


@pytest.mark.asyncio
async def test_header_denied_and_unknown_not_persisted():
    from archolith_proxy.openai.chat import _apply_session_config_overlay

    be = _fake_backend(stored="")
    base_key = get_settings().upstream_api_key  # capture before overlay activates
    header = json.dumps({
        "upstream_api_key": "EVIL",       # denylisted
        "bogus_field_xyz": 1,             # unknown
        "context_token_budget": 1010,     # valid
    })
    with patch("archolith_proxy.openai.chat.get_backend", return_value=be):
        eff = await _apply_session_config_overlay(header, "sess-2", get_settings())

    persisted = json.loads(be.set_session_config_overrides.await_args.args[1])
    assert persisted == {"context_token_budget": 1010}  # only the valid field
    assert eff.context_token_budget == 1010
    assert eff.upstream_api_key == base_key  # denylisted secret untouched
    set_session_settings(None)


@pytest.mark.asyncio
async def test_invalid_header_json_does_not_persist():
    from archolith_proxy.openai.chat import _apply_session_config_overlay

    be = _fake_backend(stored="")
    with patch("archolith_proxy.openai.chat.get_backend", return_value=be):
        eff = await _apply_session_config_overlay("not-json{{", "sess-3", get_settings())

    be.set_session_config_overrides.assert_not_awaited()
    # No stored overrides → base settings returned, no overlay.
    assert eff is not None
    set_session_settings(None)


@pytest.mark.asyncio
async def test_no_header_loads_stored_overrides():
    from archolith_proxy.openai.chat import _apply_session_config_overlay

    be = _fake_backend(stored=json.dumps({"context_token_budget": 2020}))
    with patch("archolith_proxy.openai.chat.get_backend", return_value=be):
        eff = await _apply_session_config_overlay(None, "sess-4", get_settings())

    be.set_session_config_overrides.assert_not_awaited()  # no header → no write
    assert eff.context_token_budget == 2020
    assert get_settings().context_token_budget == 2020
    set_session_settings(None)


@pytest.mark.asyncio
async def test_empty_overrides_returns_base_no_overlay():
    from archolith_proxy.openai.chat import _apply_session_config_overlay

    base = get_settings()
    be = _fake_backend(stored="")
    with patch("archolith_proxy.openai.chat.get_backend", return_value=be):
        eff = await _apply_session_config_overlay(None, "sess-5", base)

    assert eff is base  # unchanged, no overlay activated
    set_session_settings(None)


@pytest.mark.asyncio
async def test_helper_round_trip_real_ladybug_backend():
    """End-to-end through the real graph layer: header -> base64-persisted ->
    reloaded -> overlay reflects the override."""
    from archolith_proxy.graph.ladybug_backend import LadybugBackend
    from archolith_proxy.openai.chat import _apply_session_config_overlay

    with tempfile.TemporaryDirectory() as tmp:
        be = LadybugBackend(db_path=str(Path(tmp) / "t.lbug"), max_concurrent_queries=2)
        await be.connect()
        await be.ensure_schema()
        try:
            await be.create_session("real-1")
            header = json.dumps({"context_token_budget": 3030, "upstream_api_key": "EVIL"})
            with patch("archolith_proxy.openai.chat.get_backend", return_value=be):
                eff = await _apply_session_config_overlay(header, "real-1", get_settings())
            assert eff.context_token_budget == 3030
            # Denylisted secret never persisted.
            stored = json.loads(await be.get_session_config_overrides("real-1"))
            assert stored == {"context_token_budget": 3030}
        finally:
            await be.close()
            set_session_settings(None)
