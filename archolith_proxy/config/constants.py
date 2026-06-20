"""Shared config constants."""

SESSION_CONFIG_DENYLIST: frozenset[str] = frozenset({
    "upstream_base_url",
    "upstream_api_url",
    "upstream_api_key",
    "extractor_base_url",
    "extractor_api_key",
    "curator_enabled",
    "curator_base_url",
    "curator_api_key",
    "native_read_intercept_enabled",
    "embedding_base_url",
    "embedding_api_key",
    "filter_enabled",
    "synthetic_tools_enabled",
    "drop_middle_on_assembly",
    "memory_api_url",
    "memory_api_key",
    "admin_token",
    "ladybug_db_path",
    "archolith_profile",
    "log_pii_redaction_level",
    "session_consent_required",
})

_SNAPSHOT_EXCLUDE = frozenset({
    "upstream_api_key", "extractor_api_key", "embedding_api_key",
    "curator_api_key", "session_neo4j_password", "memory_api_key",
    "admin_token",
    "upstream_base_url", "extractor_base_url", "embedding_base_url",
    "curator_base_url", "prepper_base_url", "prepper_api_key",
    "assembler_base_url", "assembler_api_key",
    "session_neo4j_uri", "session_neo4j_database",
    "session_neo4j_user", "memory_api_url",
    "ladybug_db_path", "trace_dir", "promotion_audit_dir", "memory_engines_json",
    "pricing_input_per_million", "pricing_input_cached_per_million",
    "pricing_output_per_million",
})
