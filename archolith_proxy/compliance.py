"""Compliance helpers for archolith-proxy runtime behavior."""

from __future__ import annotations

import hashlib
from enum import StrEnum

from archolith_proxy.config import get_settings

try:
    from archolith_compliance.redact import PiiRedactionLevel, redact_pii
except ImportError:  # pragma: no cover - fallback only for installs without the optional extra
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


def redact_for_log(text: object) -> str:
    """Redact log-bound text using the configured PII redaction level."""
    raw_text = "" if text is None else str(text)
    level = PiiRedactionLevel(get_settings().log_pii_redaction_level)
    return redact_pii(raw_text, level)
