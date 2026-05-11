"""Application configuration via pydantic-settings.

Required env vars are validated on first access. get_settings() returns
a cached singleton — Settings() is constructed once per process.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module-level singleton
_settings: Settings | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Upstream API
    upstream_base_url: str = "https://api.deepseek.com/v1"
    upstream_api_key: str = ""

    # Extraction model
    extractor_base_url: str = "https://api.openai.com/v1"
    extractor_api_key: str = ""
    extractor_model: str = "gpt-4.1-mini"

    # Embeddings
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # Session graph (Neo4j — label-based isolation in default database)
    session_neo4j_uri: str = "bolt://localhost:7687"
    session_neo4j_database: str = "neo4j"
    session_neo4j_user: str = "neo4j"
    session_neo4j_password: str = ""

    # Proxy settings
    proxy_port: int = 9800
    coherence_tail_size: int = 3
    max_tail_messages: int = 20
    context_token_budget: int = 15000
    session_ttl_hours: int = 24
    cold_start_turns: int = 3
    cold_start_token_threshold: int = 20000

    # Embedding-driven retrieval
    embedding_enabled: bool = False

    # Context compaction (overflow fallback)
    compaction_enabled: bool = False

    # Query rewriting for ambiguous messages (resolves pronouns before embedding)
    query_rewrite_enabled: bool = False

    # Retry / resilience
    upstream_max_retries: int = 3
    upstream_retry_backoff_base_s: float = 0.5
    neo4j_max_retries: int = 3
    neo4j_retry_backoff_base_s: float = 1.0
    assembly_latency_budget_ms: int = 150

    # Optional: promotion to long-term memory
    memory_api_url: str = "http://localhost:8200"
    memory_api_key: str = ""
    promotion_enabled: bool = False

    @property
    def upstream_api_url(self) -> str:
        """Full upstream API base URL (ensures no trailing slash issues)."""
        return self.upstream_base_url.rstrip("/")

    @field_validator("upstream_api_key")
    @classmethod
    def _warn_empty_upstream_key(cls, v: str) -> str:
        if not v:
            import structlog
            structlog.get_logger().warning(
                "UPSTREAM_API_KEY is empty — proxy will fail on upstream calls. "
                "Set UPSTREAM_API_KEY in .env or environment."
            )
        return v

    @field_validator("upstream_base_url")
    @classmethod
    def _validate_upstream_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"UPSTREAM_BASE_URL must start with http:// or https://, got: {v}")
        return v

    @field_validator("proxy_port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError(f"PROXY_PORT must be 1-65535, got: {v}")
        return v

    def check_required_for_graph(self) -> list[str]:
        """Return list of missing env vars required for graph features."""
        missing = []
        if not self.session_neo4j_password:
            missing.append("SESSION_NEO4J_PASSWORD")
        if not self.extractor_api_key:
            missing.append("EXTRACTOR_API_KEY")
        return missing

    def check_required_for_proxy(self) -> list[str]:
        """Return list of missing env vars required for basic proxy."""
        missing = []
        if not self.upstream_api_key:
            missing.append("UPSTREAM_API_KEY")
        return missing


def get_settings() -> Settings:
    """Return cached settings instance (singleton per process)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset the cached settings — used in tests."""
    global _settings
    _settings = None
