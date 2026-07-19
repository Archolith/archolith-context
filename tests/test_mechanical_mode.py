"""Tests for ARCHOLITH_PROFILE flag bundles."""

from __future__ import annotations


from archolith_proxy.config import PROFILES, Settings, _apply_profile, reset_settings, snapshot_config


def test_profiles_defined() -> None:
    """All four profiles exist in the PROFILES table."""
    assert "passthrough" in PROFILES
    assert "mechanical" in PROFILES
    assert "curated" in PROFILES
    assert "full" in PROFILES


def test_mechanical_profile() -> None:
    """Mechanical profile enables filter + agent-solo strategies at 3K threshold."""
    bundle = PROFILES["mechanical"]
    assert bundle.get("filter_enabled") is True
    assert bundle.get("agent_solo_shrink_enabled") is True
    assert bundle.get("agent_solo_dedup_enabled") is True
    assert bundle.get("agent_solo_compress_middle_enabled") is True
    assert bundle.get("agent_solo_min_input_tokens") == 3000
    # Mechanical does NOT enable curator or graph features
    assert bundle.get("curator_enabled") is None


def test_passthrough_profile_empty() -> None:
    """Passthrough profile has no overrides."""
    assert PROFILES["passthrough"] == {}


def test_settings_default_profile() -> None:
    """Default archolith_profile is passthrough."""
    settings = Settings()
    assert settings.archolith_profile == "passthrough"


def test_apply_mechanical_profile() -> None:
    """Applying mechanical profile sets expected flags when env is silent."""
    settings = Settings(archolith_profile="mechanical")
    _apply_profile(settings)

    assert settings.filter_enabled is True
    assert settings.agent_solo_shrink_enabled is True
    assert settings.agent_solo_min_input_tokens == 3000
    # Curator should NOT be enabled by mechanical
    assert settings.curator_enabled is False


def test_explicit_env_beats_profile() -> None:
    """When a field is set explicitly via env, the profile does not override it."""
    # Construct with explicit value — this puts filter_enabled in model_fields_set
    settings = Settings(archolith_profile="mechanical", filter_enabled=False)
    _apply_profile(settings)

    # Explicit False should beat profile's True
    assert settings.filter_enabled is False
    # But profile's other fields should still apply
    assert settings.agent_solo_shrink_enabled is True


def test_curated_profile() -> None:
    """Curated profile includes mechanical + curator + graph features."""
    bundle = PROFILES["curated"]
    assert bundle.get("filter_enabled") is True
    assert bundle.get("curator_enabled") is True
    assert bundle.get("background_pass_enabled") is True
    assert bundle.get("file_cache_enabled") is True
    # Full-only features should not be in curated
    assert bundle.get("embedding_enabled") is None
    assert bundle.get("per_tool_extraction_enabled") is None


def test_full_profile() -> None:
    """Full profile includes all features."""
    bundle = PROFILES["full"]
    assert bundle.get("embedding_enabled") is True
    assert bundle.get("per_tool_extraction_enabled") is True
    assert bundle.get("session_recall_tool_enabled") is True


def test_unknown_profile_falls_back() -> None:
    """An unknown profile name falls back to passthrough (no-ops)."""
    settings = Settings(archolith_profile="nonexistent")
    _apply_profile(settings)

    assert settings.filter_enabled is False  # passthrough has no overrides


def test_config_default_in_snapshot_exclusions() -> None:
    """archolith_profile is NOT excluded from config snapshots (visible to /admin/config)."""
    from archolith_proxy.config import _SNAPSHOT_EXCLUDE
    assert "archolith_profile" not in _SNAPSHOT_EXCLUDE


def test_snapshot_config_includes_profile() -> None:
    """snapshot_config() includes the active profile."""
    reset_settings()
    snap = snapshot_config()
    # Should have profile in the output
    assert "archolith_profile" in snap
    # Default is passthrough unless set via env
    assert snap["archolith_profile"] in ("passthrough", "mechanical", "curated", "full")


def test_config_in_session_denylist() -> None:
    """archolith_profile is in the session config denylist."""
    from archolith_proxy.config import SESSION_CONFIG_DENYLIST
    assert "archolith_profile" in SESSION_CONFIG_DENYLIST


# ---------------------------------------------------------------------------
# Degradation tests — what happens when archolith_filter is missing
# ---------------------------------------------------------------------------


def _simulate_mechanical_degradation(settings: Settings) -> None:
    """Simulate the startup degradation path for profile-driven filter_enabled."""
    settings.archolith_profile = "passthrough"
    settings.filter_enabled = False
    settings.agent_solo_shrink_enabled = False
    settings.agent_solo_dedup_enabled = False
    settings.agent_solo_compress_middle_enabled = False
    settings.curator_enabled = False
    settings.background_pass_enabled = False
    settings.embedding_enabled = False
    settings.per_tool_extraction_enabled = False
    settings.session_recall_tool_enabled = False


def test_mechanical_degradation_clears_filter_and_agent_solo() -> None:
    """Degradation from mechanical profile clears filter + agent-solo flags."""
    settings = Settings(archolith_profile="mechanical")
    _apply_profile(settings)
    assert settings.filter_enabled is True  # profile applied
    assert settings.agent_solo_shrink_enabled is True

    _simulate_mechanical_degradation(settings)
    assert settings.filter_enabled is False
    assert settings.agent_solo_shrink_enabled is False
    assert settings.agent_solo_dedup_enabled is False
    assert settings.agent_solo_compress_middle_enabled is False


def test_curated_degradation_clears_all_curated_flags() -> None:
    """Degradation from curated profile also clears curator + background pass."""
    settings = Settings(archolith_profile="curated")
    _apply_profile(settings)
    assert settings.curator_enabled is True
    assert settings.background_pass_enabled is True
    assert settings.file_cache_enabled is True
    assert settings.filter_enabled is True

    _simulate_mechanical_degradation(settings)
    assert settings.curator_enabled is False
    assert settings.background_pass_enabled is False
    assert settings.filter_enabled is False


def test_full_degradation_clears_all_flags() -> None:
    """Degradation from full profile clears all features."""
    settings = Settings(archolith_profile="full")
    _apply_profile(settings)
    assert settings.embedding_enabled is True
    assert settings.per_tool_extraction_enabled is True
    assert settings.session_recall_tool_enabled is True

    _simulate_mechanical_degradation(settings)
    assert settings.embedding_enabled is False
    assert settings.per_tool_extraction_enabled is False
    assert settings.session_recall_tool_enabled is False


def test_degradation_sets_profile_to_passthrough() -> None:
    """Degradation sets archolith_profile to passthrough."""
    settings = Settings(archolith_profile="curated")
    _apply_profile(settings)
    assert settings.archolith_profile == "curated"

    _simulate_mechanical_degradation(settings)
    assert settings.archolith_profile == "passthrough"
