"""Filesystem paths used by config loading."""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = str(_PROJECT_ROOT / ".env")
_OVERRIDES_FILE = _PROJECT_ROOT / "config_overrides.json"
