"""Backend, memory, cache, and assembly settings."""

from pydantic import BaseModel

from archolith_proxy.config.paths import _PROJECT_ROOT


class BackendMemoryGroup(BaseModel):
    graph_backend: str = "neo4j"
    ladybug_db_path: str = str(_PROJECT_ROOT / "data" / "context.lbug")
    ladybug_max_concurrent: int = 8
    require_graph_on_startup: bool = False
    memory_api_url: str = "http://localhost:8200"
    memory_api_key: str = ""
    promotion_enabled: bool = False
    memory_engines_json: str = ""
    promotion_min_confidence: float = 0.9
    promotion_dry_run: bool = False
    promotion_audit_dir: str = ""
    file_cache_enabled: bool = True
    file_cache_max_file_bytes: int = 500_000
    file_cache_ttl_turns: int = 50
    file_cache_max_entries: int = 200
    fact_pool_limit: int = 200
    prefetch_allowed_roots: list[str] = []
    i_accept_unrestricted_fs_risk: bool = False
    prefetch_restrict_to_workspace: bool = True
    native_read_intercept_enabled: bool = True
    drop_middle_on_assembly: bool = False
    short_session_context_budget: int = 0
