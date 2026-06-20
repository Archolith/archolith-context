"""Upstream and helper API settings."""

from pydantic import BaseModel


class UpstreamGroup(BaseModel):
    upstream_base_url: str = "https://api.deepseek.com/v1"
    upstream_api_key: str = ""
    allow_insecure_upstream_url: bool = False


class ModelApiGroup(BaseModel):
    extractor_base_url: str = "https://api.openai.com/v1"
    extractor_api_key: str = ""
    extractor_model: str = "gpt-4.1-mini"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
