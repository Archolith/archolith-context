"""Curator and agent-solo settings."""

from pydantic import BaseModel, Field


class CuratorGroup(BaseModel):
    curator_enabled: bool = False
    curator_model: str = ""
    curator_base_url: str = ""
    curator_api_key: str = ""
    curator_max_iterations: int = 6
    curator_latency_budget_ms: int = 6000
    agent_solo_shrink_enabled: bool = False
    agent_solo_dedup_enabled: bool = False
    agent_solo_compress_middle_enabled: bool = False
    agent_solo_shrink_max_tokens: int = 2000
    agent_solo_min_input_tokens: int = 8000
    agent_solo_dump_payloads: bool = False
    briefing_max_staleness: int = 2
    background_pass_enabled: bool = False
    background_pass_max_iterations: int = 12
    background_pass_debounce_ms: int = 2000
    background_pass_latency_budget_ms: int = 30_000
    curation_mode: str = "two_pass"
    prepper_model: str = ""
    prepper_base_url: str = ""
    prepper_api_key: str = ""
    prepper_max_iterations: int = 12
    prepper_debounce_ms: int = 2000
    prepper_latency_budget_ms: int = 60_000
    assembler_model: str = ""
    assembler_base_url: str = ""
    assembler_api_key: str = ""
    assembler_max_iterations: int = 2
    assembler_latency_budget_ms: int = 3000
    assembler_deterministic: bool = False
    assembler_token_budget: int = 6000
    prepper_block_on_miss: bool = False
    prepper_block_budget_ms: int = 10_000
    prepper_light_max_iterations: int = 5
    curator_worker_enabled: bool = False
    curator_worker_debounce_ms: int = 2000
    curator_worker_max_queue: int = 100
    curator_worker_idle_ttl_s: int = 1800
    curator_worker_lease_enabled: bool = False
    curator_worker_lease_db_path: str = ""
    curator_worker_lease_duration_s: int = 90
    curator_state_persist_enabled: bool = False
    curator_state_persist_path: str = ""
    assembler_scored_selection: bool = False
    assembler_topological_fill: bool = False
    assembler_combo_fill: bool = False
    assembler_exemplar_suffixes: str = ""
    assembler_code_map: bool = False
    assembler_code_map_mode: str = "task"
    # Reserve at least half the assembler budget for the typed briefing and code.
    # Disable the map with assembler_code_map rather than assigning a zero budget.
    assembler_code_map_budget_fraction: float = Field(default=0.12, gt=0.0, le=0.5)
    curator_list_dir_tool: bool = False
    curator_workingset_enabled: bool = False
    curator_workingset_max_sessions: int = 256

    # Prompt Cache Stability (Phase 0+)
    context_cache_enabled: bool = False
    context_cache_max_bloat_ratio: float = 1.6
    context_cache_force_refresh_threshold_tokens: int = 12000
    provider_cache_ttl_seconds: int = 600  # 10 minutes default (tunable per provider)
