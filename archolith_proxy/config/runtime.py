"""Runtime config singleton and override helpers."""

from __future__ import annotations

import importlib
import json

from archolith_proxy.config.constants import SESSION_CONFIG_DENYLIST
from archolith_proxy.config.profiles import PROFILES
from archolith_proxy.config.settings import Settings


def _config_module():
    return importlib.import_module("archolith_proxy.config")


def _apply_profile(settings: Settings) -> None:
    profile = getattr(settings, "archolith_profile", "passthrough")
    if profile not in PROFILES:
        profile = "passthrough"
    bundle = PROFILES[profile]
    if not bundle:
        return
    explicit = getattr(settings, "model_fields_set", set())
    for key, value in bundle.items():
        if key not in explicit and hasattr(settings, key):
            setattr(settings, key, value)


def _get_global_settings() -> Settings:
    cfg = _config_module()
    if cfg._settings is None:
        with cfg._settings_lock:
            if cfg._settings is None:
                cfg._settings = Settings()
                cfg._apply_profile(cfg._settings)
                cfg._base_values = {k: getattr(cfg._settings, k) for k in type(cfg._settings).model_fields}
                cfg._apply_overrides(cfg._settings)
    return cfg._settings


def get_settings() -> Settings:
    cfg = _config_module()
    overlay = cfg._session_settings_ctx.get()
    if overlay is not None:
        return overlay
    return cfg._get_global_settings()


def build_effective_settings(overrides: dict[str, object]) -> Settings:
    base = _config_module()._get_global_settings()
    effective = base.model_copy()
    for key, value in (overrides or {}).items():
        if key in SESSION_CONFIG_DENYLIST or not hasattr(effective, key):
            continue
        try:
            expected_type = type(getattr(effective, key))
            setattr(effective, key, expected_type(value) if not isinstance(value, expected_type) else value)
        except (ValueError, TypeError):
            continue
    return effective


def set_session_settings(settings: Settings | None):
    return _config_module()._session_settings_ctx.set(settings)


def reset_session_settings(token) -> None:
    try:
        _config_module()._session_settings_ctx.reset(token)
    except (ValueError, LookupError):
        pass


def reset_settings() -> None:
    cfg = _config_module()
    with cfg._settings_lock:
        cfg._settings = None
        cfg._base_values = {}


def _read_overrides() -> dict[str, object]:
    try:
        overrides_file = _config_module()._OVERRIDES_FILE
        if overrides_file.exists():
            return json.loads(overrides_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _write_overrides(updates: dict[str, object]) -> None:
    import structlog
    cfg = _config_module()
    logger = structlog.get_logger()
    current = cfg._read_overrides()
    current.update(updates)
    try:
        cfg._OVERRIDES_FILE.write_text(json.dumps(current, indent=2, default=str), encoding="utf-8")
        logger.info("config_overrides_persisted", file=str(cfg._OVERRIDES_FILE), keys=list(updates.keys()))
    except Exception as e:
        logger.warning("config_overrides_write_failed", error=str(e))


def _apply_overrides(settings: Settings) -> None:
    for key, value in _config_module()._read_overrides().items():
        if hasattr(settings, key):
            try:
                expected_type = type(getattr(settings, key))
                setattr(settings, key, expected_type(value))
            except (ValueError, TypeError):
                pass


def get_settings_delta() -> dict[str, dict[str, object]]:
    cfg = _config_module()
    settings = cfg.get_settings()
    current = {k: getattr(settings, k) for k in type(settings).model_fields}
    overridden = [k for k in current if cfg._base_values.get(k) != current.get(k)]
    return {"base": cfg._base_values, "current": current, "overridden": overridden}


def snapshot_config() -> dict[str, object]:
    cfg = _config_module()
    settings = cfg.get_settings()
    return {k: v for k, v in settings.model_dump().items() if k not in cfg._SNAPSHOT_EXCLUDE}
