"""Graph node models for session context storage."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class FactType(str, Enum):
    FILE_STATE = "file_state"
    ERROR = "error"
    TOOL_RESULT = "tool_result"
    DECISION = "decision"
    STATE = "state"
    GOAL = "goal"
    OBSERVATION = "observation"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    PROMOTED = "promoted"


class FileStatus(str, Enum):
    READ = "read"
    MODIFIED = "modified"
    CREATED = "created"
    DELETED = "deleted"


class SessionNode(BaseModel):
    session_id: str
    # fingerprint: optional session identity fingerprint; present for regular
    # sessions, but can also persist for fallback sessions (agent-solo resumptions).
    fingerprint: str | None = None
    goal: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_hours: int = 24
    status: SessionStatus = SessionStatus.ACTIVE
    turn_number: int = 0


class FactNode(BaseModel):
    fact_id: str
    session_id: str
    content: str
    fact_type: FactType
    valid_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: datetime | None = None
    confidence: float = 0.5
    source_turn: int = 0
    embedding: list[float] | None = None


class FileNode(BaseModel):
    path: str
    session_id: str
    last_read_turn: int | None = None
    last_modified_turn: int | None = None
    status: FileStatus = FileStatus.READ


class DecisionNode(BaseModel):
    decision_id: str
    session_id: str
    summary: str
    rationale: str | None = None
    turn: int = 0
    superseded_by: str | None = None


class CheckpointNode(BaseModel):
    session_id: str
    summary: str
    next_step: str | None = None
    confidence: float = 0.5
    source_turn: int = 0


class IssueNode(BaseModel):
    issue_id: str
    session_id: str
    status: str = "open"  # "open" | "resolved"
    summary: str
    related_file: str | None = None
    related_command: str | None = None
    resolution_ref: str | None = None
    source_turn: int = 0
    resolved_turn: int = 0


class VerificationNode(BaseModel):
    verification_id: str
    session_id: str
    command: str
    status: str  # "pass" | "fail" | "partial"
    summary: str
    source_turn: int = 0
