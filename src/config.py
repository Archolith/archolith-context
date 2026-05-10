"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Optional: promotion to long-term memory
    memory_api_url: str = "http://localhost:8200"
    memory_api_key: str = ""
    promotion_enabled: bool = False

    @property
    def upstream_api_url(self) -> str:
        """Full upstream API base URL (ensures no trailing slash issues)."""
        return self.upstream_base_url.rstrip("/")


def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
