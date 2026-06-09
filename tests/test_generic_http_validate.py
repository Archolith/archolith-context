"""Tests for D7 — generic_http adapter base_url scheme validation."""

from __future__ import annotations

import pytest

from archolith_proxy.memory.adapters.generic_http import Adapter as GenericHttpAdapter
from archolith_proxy.memory.models import MemoryEngineConfig


def _adapter(base_url: str) -> GenericHttpAdapter:
    return GenericHttpAdapter(
        MemoryEngineConfig(id="t", type="generic_http", base_url=base_url)
    )


class TestGenericHttpValidateConfig:
    @pytest.mark.asyncio
    async def test_valid_https(self):
        assert await _adapter("https://memory.example.com").validate_config() == []

    @pytest.mark.asyncio
    async def test_valid_http_with_port_and_path(self):
        assert await _adapter("http://localhost:8080/api").validate_config() == []

    @pytest.mark.asyncio
    async def test_missing_base_url(self):
        problems = await _adapter("").validate_config()
        assert any("required" in p for p in problems)

    @pytest.mark.asyncio
    async def test_non_http_scheme_rejected(self):
        problems = await _adapter("ftp://memory.example.com").validate_config()
        assert any("http(s)" in p for p in problems)

    @pytest.mark.asyncio
    async def test_scheme_without_host_rejected(self):
        problems = await _adapter("https://").validate_config()
        assert any("http(s)" in p for p in problems)

    @pytest.mark.asyncio
    async def test_bare_host_no_scheme_rejected(self):
        # urlparse treats "memory.example.com" as a path with empty scheme/netloc.
        problems = await _adapter("memory.example.com").validate_config()
        assert any("http(s)" in p for p in problems)
