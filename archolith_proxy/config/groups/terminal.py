"""Pricing, trace, security, and plugin settings."""

from pydantic import BaseModel


class TerminalGroup(BaseModel):
    pricing_input_per_million: float = 0.14
    pricing_input_cached_per_million: float = 0.0028
    pricing_output_per_million: float = 0.28
    trace_dir: str = ""
    trace_retention_days: int = 0
    admin_token: str = ""
    ws_allow_anonymous: bool = False
    admin_allow_open_nonlocal: bool = False
    plugins_enabled: str = ""
    plugins_disabled: str = ""
