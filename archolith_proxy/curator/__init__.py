"""Curator public surface and curation-mode registration."""
from __future__ import annotations
from collections.abc import Awaitable, Callable
import structlog
from archolith_proxy.config import get_settings
from archolith_proxy.curator.briefing import SessionBriefing
from archolith_proxy.curator.pipeline import _build_briefing_from_result, _extract_section
from archolith_proxy.curator.pipeline import curate_context, get_last_attempt, run_background_pass
from archolith_proxy.models.dtos import AssembledContext
logger = structlog.get_logger()
BackgroundPassFn = Callable[..., Awaitable[SessionBriefing | None]]
InlinePassFn = Callable[..., Awaitable[AssembledContext | None]]
_background_pass_fn: BackgroundPassFn | None = None
_inline_pass_fn: InlinePassFn | None = None
def register_curation_mode(background_pass_fn: BackgroundPassFn | None = None,
                           inline_pass_fn: InlinePassFn | None = None) -> None:
    """Register mode-specific curation functions."""
    global _background_pass_fn, _inline_pass_fn
    if background_pass_fn is not None:
        _background_pass_fn = background_pass_fn
    if inline_pass_fn is not None:
        _inline_pass_fn = inline_pass_fn
def unregister_curation_mode() -> None:
    """Unregister all mode-specific functions, reverting to default behavior."""
    global _background_pass_fn, _inline_pass_fn
    _background_pass_fn = None
    _inline_pass_fn = None
def configure_curation_mode() -> None:
    """Register the active curation mode from settings."""
    settings = get_settings()
    if settings.curation_mode == "two_curator":
        from archolith_proxy.curator.prepper import run_prepper
        from archolith_proxy.curator.assembler import run_assembler
        register_curation_mode(background_pass_fn=run_prepper, inline_pass_fn=run_assembler)
        logger.info(
            "curation_mode_configured",
            mode="two_curator",
            prepper_model=settings.prepper_model or settings.curator_model or settings.extractor_model,
            assembler_model=settings.assembler_model or settings.curator_model or settings.extractor_model,
        )
        return
    unregister_curation_mode()
    logger.info("curation_mode_configured", mode=settings.curation_mode)
__all__ = ["curate_context", "run_background_pass", "get_last_attempt", "register_curation_mode",
           "unregister_curation_mode", "configure_curation_mode"]
