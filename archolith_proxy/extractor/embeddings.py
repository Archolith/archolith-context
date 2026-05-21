"""Batch embedding computation for extracted facts.

Calls text-embedding-3-small to compute embeddings for all facts
from a single extraction in one batch request. Falls back gracefully
if the embedding API is unavailable — facts are stored without embeddings
and the assembler uses recency-only retrieval.
"""

from __future__ import annotations

import json

import httpx
import structlog

from archolith_proxy.config import get_settings

logger = structlog.get_logger()

# Maximum texts per embedding batch request (OpenAI limit is 2048)
MAX_BATCH_SIZE = 100


async def compute_embeddings_batch(
    http_client: httpx.AsyncClient,
    texts: list[str],
) -> list[list[float] | None]:
    """Compute embeddings for a batch of texts in a single API call.

    Returns a list of embedding vectors aligned with the input texts.
    If the embedding API fails, returns None for each text (graceful fallback).
    """
    if not texts:
        return []

    settings = get_settings()

    # Skip if no API key configured
    if not settings.embedding_api_key:
        logger.debug("embedding_skipped_no_key")
        return [None] * len(texts)

    # Truncate individual texts to avoid token limits
    truncated_texts = [t[:8000] for t in texts]

    # Batch in chunks if needed
    all_embeddings: list[list[float] | None] = []

    for i in range(0, len(truncated_texts), MAX_BATCH_SIZE):
        batch = truncated_texts[i : i + MAX_BATCH_SIZE]
        try:
            resp = await http_client.post(
                f"{settings.embedding_base_url.rstrip('/')}/embeddings",
                headers={
                    "Authorization": f"Bearer {settings.embedding_api_key}",
                    "Content-Type": "application/json",
                },
                content=json.dumps({
                    "model": settings.embedding_model,
                    "input": batch,
                }).encode(),
            )
            resp.raise_for_status()
            data = resp.json()

            # Results come back in the same order as input
            embeddings_map = {}
            for item in data.get("data", []):
                idx = item.get("index", 0)
                embeddings_map[idx] = item.get("embedding", [])

            for j in range(len(batch)):
                all_embeddings.append(embeddings_map.get(j))

            logger.debug(
                "embedding_batch_computed",
                batch_size=len(batch),
                total=len(all_embeddings),
            )

        except Exception as e:
            logger.warning(
                "embedding_batch_failed",
                batch_start=i,
                batch_size=len(batch),
                error=str(e),
            )
            # Graceful fallback: no embeddings for this batch
            all_embeddings.extend([None] * len(batch))

    return all_embeddings
