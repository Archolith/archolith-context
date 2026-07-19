"""Proxy, session graph, feature, and retry settings."""

from pydantic import BaseModel


class SessionGraphGroup(BaseModel):
    session_neo4j_uri: str = "bolt://localhost:7687"
    session_neo4j_database: str = "neo4j"
    session_neo4j_user: str = "neo4j"
    session_neo4j_password: str = ""


class ProxyBehaviorGroup(BaseModel):
    proxy_port: int = 9800
    proxy_host: str = "127.0.0.1"
    cors_allowed_origins: list[str] = []
    coherence_tail_size: int = 10
    max_tail_messages: int = 20
    tail_intent_enabled: bool = False
    tail_intent_adjustment: int = 4
    tail_min_size: int = 3
    context_token_budget: int = 15000
    max_rewritten_tokens: int = 24000
    session_ttl_hours: int = 24
    cold_start_turns: int = 3
    cold_start_token_threshold: int = 20000
    assembly_min_savings_ratio: float = 0.25
    assembly_min_input_tokens: int = 55000


class FeatureRuntimeGroup(BaseModel):
    embedding_enabled: bool = False
    compaction_enabled: bool = False
    query_rewrite_enabled: bool = False
    session_recall_tool_enabled: bool = False
    streaming_recall_decision_timeout_s: float = 5.0
    synthetic_tools_enabled: bool = False
    synthetic_circuit_max_consecutive: int = 3
    synthetic_circuit_cooldown_s: float = 300.0
    synthetic_circuit_max_total: int = 10
    max_input_tokens_per_session: int = 2_000_000
    session_token_budget_action: str = "passthrough"
    extraction_mode: str = "turn_boundary"
    per_tool_extraction_enabled: bool = False
    extractor_llm_concurrency: int = 3


class ProfileFilterRetryGroup(BaseModel):
    archolith_profile: str = "passthrough"
    filter_enabled: bool = False
    upstream_max_retries: int = 3
    upstream_retry_backoff_base_s: float = 0.5
    neo4j_max_retries: int = 3
    neo4j_retry_backoff_base_s: float = 1.0
    assembly_latency_budget_ms: int = 150
