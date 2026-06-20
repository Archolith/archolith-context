"""Compliance helpers for archolith-proxy runtime behavior."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from enum import StrEnum
from collections.abc import Mapping

from archolith_proxy.config import get_settings

try:
    from archolith_compliance.consent import ConsentState
    from archolith_compliance.redact import PiiRedactionLevel, redact_pii
except ImportError:  # pragma: no cover - fallback only for installs without the optional extra
    class ConsentState(StrEnum):
        UNKNOWN = "unknown"
        OPTED_IN = "opted_in"
        OPTED_OUT = "opted_out"

    class PiiRedactionLevel(StrEnum):
        NONE = "none"
        TRUNCATED_32 = "truncated_32"
        HASHED = "hashed"
        REDACTED = "redacted"

    def redact_pii(text: str, level: PiiRedactionLevel) -> str:
        if level == PiiRedactionLevel.NONE:
            return text
        if level == PiiRedactionLevel.TRUNCATED_32:
            return text if len(text) <= 32 else f"{text[:32]}..."
        if level == PiiRedactionLevel.HASHED:
            return f"[sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}]"
        return "[redacted]"


_trace_recording_allowed: ContextVar[bool] = ContextVar("trace_recording_allowed", default=True)


def redact_for_log(text: object) -> str:
    """Redact log-bound text using the configured PII redaction level."""
    raw_text = "" if text is None else str(text)
    level = PiiRedactionLevel(get_settings().log_pii_redaction_level)
    return redact_pii(raw_text, level)


def apply_session_consent(headers: Mapping[str, str]) -> ConsentState:
    """Set request-local trace recording consent from headers."""
    settings = get_settings()
    raw = (headers.get("x-session-consent") or headers.get("X-Session-Consent") or "").strip().lower()
    if raw in {"opt-in", "opted-in", "true", "1", "yes"}:
        state = ConsentState.OPTED_IN
    elif raw in {"opt-out", "opted-out", "false", "0", "no"}:
        state = ConsentState.OPTED_OUT
    else:
        state = ConsentState.UNKNOWN

    allowed = (not settings.session_consent_required) or state == ConsentState.OPTED_IN
    _trace_recording_allowed.set(allowed)
    return state


def trace_recording_allowed() -> bool:
    """Return whether trace-store writes are allowed in the current request context."""
    return _trace_recording_allowed.get()
