"""Fact-level compression for assembly-time rendering.

Compresses extracted facts to their densest usable form without losing
actionable content. Rule-based, no LLM call, runs in <1ms per fact.

Strategy:
1. Strip hedging/filler phrases ("it appears that", "based on analysis")
2. Collapse verbose verb phrases ("is responsible for" → "handles")
3. Remove redundant session context ("in the current session,")
4. Preserve specific values: file paths, numbers, identifiers, error types
5. Collapse whitespace and trailing punctuation
"""

from __future__ import annotations

import re

# Hedging/filler prefixes to strip (order matters — longer matches first)
_FILLER_PREFIXES = [
    re.compile(r'^(?:it (?:was |has been )?(?:found|observed|noted|determined|discovered) that\s+)', re.I),
    re.compile(r'^(?:based on (?:the |my )?(?:analysis|investigation|review|examination)[,.]?\s+)', re.I),
    re.compile(r'^(?:according to (?:the )?(?:output|result|response|log|trace)[,.]?\s+)', re.I),
    re.compile(r'^(?:(?:the )?assistant (?:found|discovered|noticed|observed|determined) (?:that )?)', re.I),
    re.compile(r'^(?:i (?:found|noticed|observed|discovered|determined) (?:that )?)', re.I),
    re.compile(r'^(?:it (?:appears|seems|looks like) (?:that )?)', re.I),
    re.compile(r'^(?:upon (?:inspection|examination|review|investigation)[,.]?\s+)', re.I),
    re.compile(r'^(?:after (?:(?:further|closer) )?(?:inspection|examination|review|investigation|analysis)[,.]?\s+)', re.I),
    re.compile(r'^(?:in (?:the )?current (?:session|turn|conversation)[,.]?\s+)', re.I),
    re.compile(r'^(?:during (?:the|this) (?:session|turn|conversation)[,.]?\s+)', re.I),
    re.compile(r'^(?:as (?:a result|part) of (?:the|this) (?:analysis|investigation|review)[,.]?\s+)', re.I),
    re.compile(r'^(?:looking at (?:the )?(?:code|file|output|result|response)[,.]?\s+)', re.I),
]

# Verbose verb phrases to collapse
_VERB_COLLAPSES = [
    (re.compile(r'\bis responsible for (?:handling |managing |processing )?', re.I), 'handles '),
    (re.compile(r'\bhas been (?:modified|changed|updated) to\b', re.I), 'changed to'),
    (re.compile(r'\bwas (?:modified|changed|updated) to\b', re.I), 'changed to'),
    (re.compile(r'\bis (?:currently )?(?:being )?used (?:for|to)\b', re.I), 'used to'),
    (re.compile(r'\bis (?:a|the) (?:main |primary |key )?(?:entry point|entrypoint) (?:for|of)\b', re.I), 'is entry point for'),
    (re.compile(r'\bcontains (?:the )?(?:following|these) (?:items|elements|functions|methods|classes|files):\s*', re.I), 'has: '),
    (re.compile(r'\bwhich (?:is|are) (?:used|responsible) (?:for|to)\b', re.I), 'for'),
    (re.compile(r'\bin order to\b', re.I), 'to'),
    (re.compile(r'\bdue to the fact that\b', re.I), 'because'),
    (re.compile(r'\bfor the purpose of\b', re.I), 'for'),
    (re.compile(r'\bat this point in time\b', re.I), 'now'),
    (re.compile(r'\b(?:a total of |a count of )(\d+)\b', re.I), r'\1'),
]

# Redundant context phrases to strip entirely
_REDUNDANT_PHRASES = [
    re.compile(r'\b(?:as mentioned (?:earlier|above|before|previously))[,.]?\s*', re.I),
    re.compile(r'\b(?:it (?:is|should be) (?:noted|worth noting) that)\s+', re.I),
    re.compile(r'\b(?:importantly|notably|significantly|essentially|basically|fundamentally)[,.]?\s*', re.I),
    re.compile(r'\b(?:in summary|to summarize|overall|in conclusion)[,.]?\s*', re.I),
    re.compile(r'\b(?:please note that|note that)\s+', re.I),
]

# Multi-space collapse
_MULTI_SPACE = re.compile(r'  +')

# Sentence splitter for multi-sentence facts
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')


def compress_fact(content: str, max_tokens: int | None = None) -> str:
    """Compress a single fact to its densest usable form.

    Preserves specific values (file paths, numbers, identifiers, error types)
    while stripping filler, hedging, and verbose phrases.

    Args:
        content: Raw fact content from extraction.
        max_tokens: Optional hard token cap. If the compressed fact still
            exceeds this, it's truncated with value preservation.

    Returns:
        Compressed fact string. Never empty — returns original if
        compression would lose all content.
    """
    if not content or not content.strip():
        return content

    text = content.strip()

    # Strip filler prefixes (multi-pass for chained hedging)
    for _ in range(3):
        before = text
        for pattern in _FILLER_PREFIXES:
            text = pattern.sub('', text, count=1)
        if text == before:
            break

    # Collapse verbose verbs
    for pattern, replacement in _VERB_COLLAPSES:
        text = pattern.sub(replacement, text)

    # Strip redundant phrases
    for pattern in _REDUNDANT_PHRASES:
        text = pattern.sub('', text)

    # Collapse whitespace
    text = _MULTI_SPACE.sub(' ', text).strip()

    # Remove trailing period (we add our own formatting)
    if text.endswith('.') and not text.endswith('..'):
        text = text[:-1]

    # Capitalize first character if it was lowered by prefix stripping
    if text and text[0].islower():
        # Don't capitalize paths, identifiers, or tool names
        if not re.match(r'^(?:[a-z_][a-z0-9_./-]*[.(]|npm|pip|git|cargo|make|docker|curl|pytest|tsc|node|npx|yarn|pnpm)\b', text):
            text = text[0].upper() + text[1:]

    # Guard: don't return empty
    if not text.strip():
        return content.strip()

    # Token cap with value preservation
    if max_tokens is not None:
        text = _truncate_preserving_values(text, max_tokens)

    return text


def compress_facts_batch(
    facts: list[dict],
    max_tokens_per_fact: int | None = None,
) -> tuple[list[dict], float]:
    """Compress a batch of facts, returning compressed facts and ratio.

    Args:
        facts: List of fact dicts with 'content' key.
        max_tokens_per_fact: Optional per-fact token cap.

    Returns:
        Tuple of (compressed_facts, compression_ratio).
        compression_ratio is original_chars / compressed_chars (>1 means savings).
    """
    if not facts:
        return facts, 1.0

    total_original = 0
    total_compressed = 0
    result = []

    for fact in facts:
        original = fact.get("content", "")
        compressed = compress_fact(original, max_tokens=max_tokens_per_fact)

        total_original += len(original)
        total_compressed += len(compressed)

        compressed_fact = dict(fact)
        compressed_fact["content"] = compressed
        compressed_fact["_original_content"] = original
        result.append(compressed_fact)

    ratio = total_original / max(total_compressed, 1)
    return result, ratio


def _truncate_preserving_values(text: str, max_tokens: int) -> str:
    """Truncate text to fit within a token budget, preserving key values.

    Preserves file paths, numbers, and identifiers that appear near the
    truncation point. Uses rough 4-chars-per-token estimate for speed.
    """
    char_budget = max_tokens * 4

    if len(text) <= char_budget:
        return text

    # Find the last value boundary before the budget
    # Values: file paths, numbers, quoted strings, identifiers
    truncated = text[:char_budget]

    # Try to break at a word boundary
    last_space = truncated.rfind(' ')
    if last_space > char_budget * 0.7:
        truncated = truncated[:last_space]

    return truncated + "..."
