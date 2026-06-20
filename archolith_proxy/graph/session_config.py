"""Helpers for per-session config override JSON."""

from __future__ import annotations

import json
from collections.abc import Iterable


def _loads_object(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def merge_session_config_json(
    existing_json: str,
    patch_json: str,
    denylist: Iterable[str],
    allowlist_keys: Iterable[str],
) -> str:
    """Merge a session config patch into existing JSON with denylist/allowlist filtering."""
    merged = _loads_object(existing_json)
    patch = _loads_object(patch_json)
    denied = set(denylist)
    allowed = set(allowlist_keys)
    applied = {k: v for k, v in patch.items() if k not in denied and k in allowed}
    if applied:
        merged.update(applied)
    return json.dumps(merged)
