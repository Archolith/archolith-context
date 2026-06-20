"""Application configuration via pydantic-settings."""

from __future__ import annotations

import threading
from contextvars import ContextVar

from archolith_proxy.config.constants import SESSION_CONFIG_DENYLIST, _SNAPSHOT_EXCLUDE
from archolith_proxy.config.paths import _ENV_FILE, _OVERRIDES_FILE, _PROJECT_ROOT
from archolith_proxy.config.profiles import PROFILES
from archolith_proxy.config.runtime import (
    _apply_overrides,
    _apply_profile,
    _get_global_settings,
    _read_overrides,
    _write_overrides,
    build_effective_settings,
    get_settings,
    get_settings_delta,
    reset_session_settings,
    reset_settings,
    set_session_settings,
    snapshot_config,
)
from archolith_proxy.config.settings import Settings, _is_loopback_host, _is_non_loopback_http_url

_settings: Settings | None = None
_base_values: dict[str, object] = {}
_settings_lock = threading.Lock()
_session_settings_ctx: ContextVar[Settings | None] = ContextVar("session_settings", default=None)

__all__ = [
    "PROFILES",
    "SESSION_CONFIG_DENYLIST",
    "Settings",
    "_ENV_FILE",
    "_OVERRIDES_FILE",
    "_PROJECT_ROOT",
    "_SNAPSHOT_EXCLUDE",
    "_apply_overrides",
    "_apply_profile",
    "_base_values",
    "_get_global_settings",
    "_is_loopback_host",
    "_is_non_loopback_http_url",
    "_read_overrides",
    "_session_settings_ctx",
    "_settings",
    "_settings_lock",
    "_write_overrides",
    "build_effective_settings",
    "get_settings",
    "get_settings_delta",
    "reset_session_settings",
    "reset_settings",
    "set_session_settings",
    "snapshot_config",
]
