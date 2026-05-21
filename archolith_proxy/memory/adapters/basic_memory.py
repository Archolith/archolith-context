"""basic-memory (Obsidian) adapter — promotes facts as markdown files in an Obsidian-compatible vault.

basic-memory stores knowledge as structured Markdown files with YAML frontmatter,
observations, and wiki-link relations. It has a REST API (via `basic-memory sync --watch`
or cloud) but also supports direct filesystem writes, which is the most common
local-first pattern.

This adapter writes promoted facts as markdown files to a configured vault directory,
making them immediately visible in Obsidian or any markdown editor.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import structlog

from archolith_proxy.memory.adapters.base import MemoryAdapterBase
from archolith_proxy.memory.models import (
    EngineCapabilities,
    PromotionOutcome,
    PromotionResult,
)

if TYPE_CHECKING:
    from archolith_proxy.memory.models import MemoryEngineConfig, PromotionRecord

logger = structlog.get_logger()


class Adapter(MemoryAdapterBase):
    """Promotes facts into a basic-memory / Obsidian vault.

    Supports two modes:
    1. **Filesystem mode** (default): writes .md files directly to a vault directory.
       Set base_url to a local directory path (e.g. "C:/Users/me/basic-memory" or "/home/me/basic-memory").
    2. **API mode**: writes via basic-memory's REST API (requires basic-memory cloud or
       running `basic-memory sync --watch` with API enabled).

    Expected engine config:
    - base_url: local vault path (filesystem) or API URL (API mode)
    - extra["mode"]: "filesystem" (default) or "api"
    - extra["folder"]: subfolder within vault (default: "promoted")
    """

    def __init__(self, config: MemoryEngineConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    @property
    def _mode(self) -> str:
        return self.config.extra.get("mode", "filesystem")

    @property
    def _folder(self) -> str:
        return self.config.extra.get("folder", "promoted")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            api_key = self.config.resolved_api_key
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.config.base_url:
            problems.append("base_url is required (vault path or API URL)")
        if self._mode == "filesystem":
            path = Path(self.config.base_url)
            if path.exists() and not path.is_dir():
                problems.append(f"base_url path exists but is not a directory: {self.config.base_url}")
        return problems

    async def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            promote_fact=True,
            promote_batch=True,
            dedupe_lookup=False,
            list_by_source=True if self._mode == "api" else False,
            update_promoted=False,
            delete_promoted=False,
            healthcheck=True,
        )

    async def healthcheck(self) -> bool:
        if self._mode == "filesystem":
            return Path(self.config.base_url).is_dir()
        else:
            try:
                client = self._get_client()
                resp = await client.get("/health")
                return resp.status_code == 200
            except Exception:
                return False

    async def promote_fact(self, promotion: PromotionRecord) -> PromotionResult:
        start = time.monotonic()
        try:
            if self._mode == "filesystem":
                result = await self._write_file(promotion)
            else:
                result = await self._write_api(promotion)
            result.elapsed_ms = (time.monotonic() - start) * 1000
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return PromotionResult(
                promotion_id=promotion.promotion_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=str(exc),
                elapsed_ms=elapsed_ms,
            )

    async def promote_batch(self, promotions: list[PromotionRecord]) -> list[PromotionResult]:
        return [await self.promote_fact(p) for p in promotions]

    # --- Filesystem mode ---

    async def _write_file(self, promotion: PromotionRecord) -> PromotionResult:
        """Write a promoted fact as a markdown file in the vault."""
        import asyncio

        vault_path = Path(self.config.base_url)
        folder_path = vault_path / self._folder
        folder_path.mkdir(parents=True, exist_ok=True)

        # Generate a filename-safe slug from the promotion
        slug = self._slugify(f"{promotion.fact_type}-{promotion.promotion_id}")
        file_path = folder_path / f"{slug}.md"

        content = self._build_markdown(promotion)

        # Write file in a thread to avoid blocking
        await asyncio.to_thread(file_path.write_text, content, encoding="utf-8")

        remote_id = f"{self._folder}/{slug}.md"
        logger.info(
            "basic_memory_file_written",
            promotion_id=promotion.promotion_id,
            path=str(file_path),
        )

        return PromotionResult(
            promotion_id=promotion.promotion_id,
            engine_id=self.config.id,
            outcome=PromotionOutcome.SUCCESS,
            remote_id=remote_id,
        )

    def _build_markdown(self, promotion: PromotionRecord) -> str:
        """Build basic-memory compatible markdown with frontmatter, observations, and relations."""
        lines: list[str] = []

        # YAML frontmatter
        lines.append("---")
        lines.append(f"title: {self._escape_yaml(promotion.content[:80])}")
        lines.append(f"type: note")
        lines.append(f"permalink: {self._slugify(promotion.content[:60])}")
        lines.append(f"tags:")
        for tag in (promotion.tags or []):
            lines.append(f"  - {tag}")
        if promotion.fact_type:
            lines.append(f"  - fact:{promotion.fact_type}")
        if promotion.session_id:
            lines.append(f"  - session:{promotion.session_id}")
        lines.append(f"promoted_at: {promotion.promoted_at}")
        lines.append(f"promotion_id: {promotion.promotion_id}")
        lines.append(f"confidence: {promotion.confidence}")
        if promotion.source_turn:
            lines.append(f"source_turn: {promotion.source_turn}")
        if promotion.dedupe_key:
            lines.append(f"dedupe_key: {promotion.dedupe_key}")
        lines.append("---")
        lines.append("")

        # Content
        lines.append(f"# {promotion.content[:80]}")
        lines.append("")

        # Observation
        lines.append("## Observations")
        lines.append("")
        category = promotion.fact_type or "observation"
        lines.append(f"- [{category}] {promotion.content}")
        if promotion.decision_context:
            lines.append(f"- [rationale] {promotion.decision_context}")
        if promotion.session_goal:
            lines.append(f"- [goal] {promotion.session_goal}")
        lines.append("")

        # Relations
        if promotion.touched_files:
            lines.append("## Relations")
            lines.append("")
            for f in promotion.touched_files:
                safe_name = Path(f).stem
                lines.append(f"- touches [[{safe_name}]]")
            lines.append("")

        # Provenance
        lines.append("## Provenance")
        lines.append("")
        lines.append(f"- [source] archolith-proxy promotion")
        lines.append(f"- [session] {promotion.session_id}")
        lines.append(f"- [turn] {promotion.source_turn}")
        if promotion.promotion_reason:
            lines.append(f"- [reason] {promotion.promotion_reason}")
        if promotion.source_trace_ref:
            lines.append(f"- [trace] {promotion.source_trace_ref}")
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _slugify(text: str) -> str:
        """Convert text to a URL/filename-safe slug."""
        import re
        slug = text.lower().strip()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        return slug[:60].strip("-")

    @staticmethod
    def _escape_yaml(text: str) -> str:
        """Escape text for safe YAML value."""
        if any(c in text for c in (":", "'", '"', "\n", "#")):
            return f'"{text.replace(chr(34), chr(92) + chr(34))}"'
        return text

    # --- API mode ---

    async def _write_api(self, promotion: PromotionRecord) -> PromotionResult:
        """Write via basic-memory REST API (write_note tool)."""
        client = self._get_client()
        payload = {
            "title": promotion.content[:80],
            "content": promotion.content,
            "folder": self._folder,
            "tags": promotion.tags + [f"fact:{promotion.fact_type}", f"session:{promotion.session_id}"],
        }
        resp = await client.post("/api/notes", json=payload)
        elapsed_ms = 0  # Caller sets this

        if resp.status_code in (200, 201):
            data = resp.json()
            remote_id = data.get("permalink", "")
            return PromotionResult(
                promotion_id=promotion.promotion_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.SUCCESS,
                remote_id=remote_id,
            )
        else:
            return PromotionResult(
                promotion_id=promotion.promotion_id,
                engine_id=self.config.id,
                outcome=PromotionOutcome.FAILED,
                error_message=f"HTTP {resp.status_code}: {resp.text[:500]}",
            )
