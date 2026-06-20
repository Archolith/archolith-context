"""Compliance redaction settings and helpers."""

import pytest

from archolith_proxy.compliance import redact_for_log
from archolith_proxy.config import Settings, reset_settings
from archolith_proxy.config.constants import SESSION_CONFIG_DENYLIST


def test_log_pii_redaction_level_default():
    settings = Settings(_env_file=None)
    assert settings.log_pii_redaction_level == "truncated_32"


def test_log_pii_redaction_level_validation():
    with pytest.raises(Exception):
        Settings(log_pii_redaction_level="invalid", _env_file=None)


def test_log_pii_redaction_level_is_not_session_overridable():
    assert "log_pii_redaction_level" in SESSION_CONFIG_DENYLIST


def test_redact_for_log_uses_default_truncation(monkeypatch):
    monkeypatch.delenv("LOG_PII_REDACTION_LEVEL", raising=False)
    reset_settings()

    assert redact_for_log("abcdefghijklmnopqrstuvwxyz0123456789") == "abcdefghijklmnopqrstuvwxyz012345..."


def test_redact_for_log_uses_configured_level(monkeypatch):
    monkeypatch.setenv("LOG_PII_REDACTION_LEVEL", "redacted")
    reset_settings()

    assert redact_for_log("private user message") == "[redacted]"
