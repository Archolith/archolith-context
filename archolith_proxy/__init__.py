"""Archolith context proxy — AI-powered context intelligence."""

try:
    import importlib.metadata as _metadata
    __version__ = _metadata.version("archolith-proxy")
except Exception:
    __version__ = "0.0.0-dev"

__all__ = ["__version__"]
