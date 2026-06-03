"""Shared text-level utilities with no layer-specific dependencies.

Functions here may be imported by any layer (proxy, curator, graph, extractor)
without creating cross-layer coupling.
"""

from __future__ import annotations

import re


# ── Text normalization for deduplication ──────────────────────────────────


def _normalize(text: str) -> str:
    """Normalize fact content for comparison: lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    text = text.strip("\"'")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[.!?;:]+$", "", text)
    return text


def _tokenize(text: str) -> set[str]:
    """Split normalized text into a set of word tokens."""
    return set(_normalize(text).split())


def jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two fact strings.

    Returns a value between 0.0 (no overlap) and 1.0 (identical token sets).
    """
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


# ── Source file outline ──────────────────────────────────────────────────


def _build_outline(content: str, path: str) -> str:
    """Build a compact structural outline of a source file with line numbers."""
    symbols: list[tuple[int, str]] = []

    if path.endswith(".py"):
        try:
            import ast as _ast
            tree = _ast.parse(content)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.AsyncFunctionDef):
                    symbols.append((node.lineno, f"async def {node.name}"))
                elif isinstance(node, _ast.FunctionDef):
                    symbols.append((node.lineno, f"def {node.name}"))
                elif isinstance(node, _ast.ClassDef):
                    symbols.append((node.lineno, f"class {node.name}"))
        except Exception:
            pass

    if not symbols and path.endswith(".java"):
        try:
            import javalang
            tree = javalang.parse.parse(content)
            for _, node in tree.filter(javalang.tree.TypeDeclaration):
                if node.position:
                    kind = type(node).__name__.replace("Declaration", "").lower()
                    symbols.append((node.position.line, f"{kind} {node.name}"))
            for _, node in tree.filter(javalang.tree.ConstructorDeclaration):
                if node.position:
                    symbols.append((node.position.line, f"constructor {node.name}"))
            for _, node in tree.filter(javalang.tree.MethodDeclaration):
                if node.position:
                    mods = " ".join(sorted(node.modifiers)) if node.modifiers else ""
                    ret = node.return_type.name if node.return_type else "void"
                    label = f"{mods} {ret} {node.name}".strip()
                    symbols.append((node.position.line, label))
        except Exception:
            pass

    if not symbols:
        _PATTERNS = [
            (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
            (re.compile(r"^\s*(?:export\s+(?:default\s+)?)?class\s+(\w+)"), "class"),
            (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\("), "const"),
            (re.compile(r"^\s*def\s+(\w+)"), "def"),
            (re.compile(r"^\s*class\s+(\w+)"), "class"),
            (re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default)\s+)*(?:[\w<>\[\]?,\s]+)\s+(\w+)\s*\("), "method"),
            (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:class|interface|enum|record)\s+(\w+)"), "class"),
        ]
        for i, line in enumerate(content.split("\n"), 1):
            for pattern, kind in _PATTERNS:
                m = pattern.match(line)
                if m:
                    symbols.append((i, f"{kind} {m.group(1)}"))
                    break

    if not symbols:
        return ""

    symbols.sort(key=lambda x: x[0])
    return "\n".join(f"line {ln}: {sym}" for ln, sym in symbols)
