"""Unit tests for agent-solo session-state pruning helpers."""

from archolith_proxy.proxy.agent_solo import (
    _CuratorCache,
    _curator_caches,
    _session_trackers,
    prune_session_state,
)


class TestAgentSoloStatePruning:
    def setup_method(self):
        _session_trackers.clear()
        _curator_caches.clear()

    def test_prunes_only_inactive_sessions(self):
        _session_trackers["active"] = object()
        _session_trackers["stale"] = object()
        _curator_caches["active"] = _CuratorCache(1, "fp-active", [])
        _curator_caches["stale"] = _CuratorCache(1, "fp-stale", [])

        pruned = prune_session_state({"active"})

        assert pruned == 1
        assert "active" in _session_trackers
        assert "active" in _curator_caches
        assert "stale" not in _session_trackers
        assert "stale" not in _curator_caches
