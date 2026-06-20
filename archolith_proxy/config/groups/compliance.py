"""Compliance-related settings."""

from pydantic import BaseModel


class ComplianceGroup(BaseModel):
    log_pii_redaction_level: str = "truncated_32"
    session_consent_required: bool = False
