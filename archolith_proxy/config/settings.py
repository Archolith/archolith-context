"""Settings model and validation."""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from archolith_proxy.config.groups.api import ModelApiGroup, UpstreamGroup
from archolith_proxy.config.groups.backend import BackendMemoryGroup
from archolith_proxy.config.groups.compliance import ComplianceGroup
from archolith_proxy.config.groups.curator import CuratorGroup
from archolith_proxy.config.groups.proxy import (
    FeatureRuntimeGroup,
    ProfileFilterRetryGroup,
    ProxyBehaviorGroup,
    SessionGraphGroup,
)
from archolith_proxy.config.groups.terminal import TerminalGroup
from archolith_proxy.config.paths import _ENV_FILE


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_non_loopback_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "http" and not _is_loopback_host(parsed.hostname)


class Settings(
    BaseSettings,
    ComplianceGroup,
    TerminalGroup,
    CuratorGroup,
    BackendMemoryGroup,
    ProfileFilterRetryGroup,
    FeatureRuntimeGroup,
    ProxyBehaviorGroup,
    SessionGraphGroup,
    ModelApiGroup,
    UpstreamGroup,
):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
    )

    @property
    def upstream_api_url(self) -> str:
        """Full upstream API base URL (ensures no trailing slash issues)."""
        return self.upstream_base_url.rstrip("/")

    @property
    def cors_origin_regex(self) -> str:
        """Default loopback browser origins allowed when no explicit list is set."""
        return r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"

    @property
    def insecure_http_base_urls(self) -> list[str]:
        """Configured non-loopback HTTP base URLs that require explicit opt-in."""
        insecure = []
        for name in (
            "upstream_base_url",
            "extractor_base_url",
            "embedding_base_url",
            "curator_base_url",
            "prepper_base_url",
        ):
            value = getattr(self, name, "")
            if value and _is_non_loopback_http_url(value):
                insecure.append(name)
        return insecure

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

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        return _parse_string_list(v)

    @field_validator("log_pii_redaction_level")
    @classmethod
    def _validate_log_pii_redaction_level(cls, v: str) -> str:
        allowed = {"none", "truncated_32", "hashed", "redacted"}
        if v not in allowed:
            raise ValueError(
                "log_pii_redaction_level must be one of: "
                f"{', '.join(sorted(allowed))}"
            )
        return v

    @field_validator("prefetch_allowed_roots", mode="before")
    @classmethod
    def _parse_prefetch_allowed_roots(cls, v):
        return _parse_string_list(v)

    @field_validator(
        "upstream_base_url",
        "extractor_base_url",
        "embedding_base_url",
        "curator_base_url",
        "prepper_base_url",
    )
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        if v and not v.startswith(("http://", "https://")):
            raise ValueError(f"Base URLs must start with http:// or https://, got: {v}")
        return v

    @model_validator(mode="after")
    def _validate_plaintext_base_urls(self) -> "Settings":
        insecure = self.insecure_http_base_urls
        if not insecure or self.allow_insecure_upstream_url:
            return self
        raise ValueError(
            "Base URL setting(s) use plaintext http for a non-loopback host: "
            f"{', '.join(insecure)}. Use https://, point to localhost/127.0.0.1/::1, "
            "or set ALLOW_INSECURE_UPSTREAM_URL=true to opt into sending API keys over plaintext HTTP."
        )

    @field_validator("proxy_port")
    @classmethod
    def _validate_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError(f"PROXY_PORT must be 1-65535, got: {v}")
        return v

    def check_required_for_graph(self) -> list[str]:
        missing = []
        if not self.session_neo4j_password:
            missing.append("SESSION_NEO4J_PASSWORD")
        if not self.extractor_api_key:
            missing.append("EXTRACTOR_API_KEY")
        return missing

    def check_required_for_proxy(self) -> list[str]:
        missing = []
        if not self.upstream_api_key:
            missing.append("UPSTREAM_API_KEY")
        return missing


def _parse_string_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        if v.strip().startswith("["):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in v.split(",") if item.strip()]
    return v
