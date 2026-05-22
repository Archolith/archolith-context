"""Canonical promotion models — the single payload shape the proxy emits before adapter translation.

All adapters receive a PromotionRecord and translate it into their backend's
write API. No vendor-specific schemas leak into the promotion pipeline.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from enum import Enum

from pydantic import BaseModel, Field


class PromotionOutcome(str, Enum):
    """Status of a single promotion attempt."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # Policy filtered out or dedupe hit
    RETRY = "retry"  # Transient failure, eligible for retry


class PromotionRecord(BaseModel):
    """Canonical promotion payload — one durable fact being promoted.

    This is the narrow write-side contract. Adapters translate fields
    into their target schema but never extend this model.
    """

    # Identity
    promotion_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    session_id: str = ""
    source_turn: int = 0

    # Content
    fact_type: str = "observation"  # Matches FactType values
    content: str = ""
    confidence: float = 0.0
    session_goal: str | None = None

    # Provenance
    touched_files: list[str] = Field(default_factory=list)
    decision_context: str | None = None  # Rationale if fact_type == decision
    promotion_reason: str = ""  # Why this fact was promoted
    promoted_at: float = Field(default_factory=time.time)

    # Tags & dedup
    tags: list[str] = Field(default_factory=list)
    dedupe_key: str = ""
    source_trace_ref: str | None = None  # TurnTrace.turn_id for audit trail

    def compute_dedupe_key(self) -> str:
        """Generate a deterministic dedupe key from session + content hash.

        The same fact from the same session should produce the same key,
        preventing repeated promotions without intent.
        """
        raw = f"{self.session_id}:{self.fact_type}:{self.content}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def with_auto_dedupe(self) -> PromotionRecord:
        """Return a copy with dedupe_key populated if empty."""
        if not self.dedupe_key:
            updated = self.model_copy()
            updated.dedupe_key = self.compute_dedupe_key()
            return updated
        return self


class PromotionResult(BaseModel):
    """Result of a single promotion attempt through an adapter."""

    promotion_id: str = ""
    engine_id: str = ""
    outcome: PromotionOutcome = PromotionOutcome.PENDING
    remote_id: str | None = None  # ID in the target memory system
    error_message: str | None = None
    elapsed_ms: float = 0.0


class EngineCapabilities(BaseModel):
    """What a memory engine adapter supports.

    Unsupported capabilities degrade explicitly, not silently.
    """

    promote_fact: bool = True
    promote_batch: bool = True
    dedupe_lookup: bool = False
    list_by_source: bool = False
    update_promoted: bool = False
    delete_promoted: bool = False
    healthcheck: bool = True


class MemoryEngineConfig(BaseModel):
    """Configuration for a single memory engine.

    Engines are loaded from config (env / YAML / JSON), not code edits.
    """

    id: str
    type: str  # Adapter type: "archolith_memory", "mem0", "zep", "generic_http"
    enabled: bool = True
    priority: int = 0  # Higher = preferred default
    base_url: str = ""
    api_key_env: str = ""  # Name of env var holding the API key
    extra: dict = Field(default_factory=dict)  # Adapter-specific config

    @property
    def resolved_api_key(self) -> str:
        """Resolve the API key from the environment variable name."""
        import os

        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""
