"""Shared utilities — cross-layer functions with no layer-specific dependencies."""

from archolith_proxy.shared.text_utils import (
    slugify,
    normalize_text,
    tokenize_text,
    build_outline,
    jaccard_similarity,
)

__all__ = [
    "slugify",
    "normalize_text",
    "tokenize_text",
    "build_outline",
    "jaccard_similarity",
]
