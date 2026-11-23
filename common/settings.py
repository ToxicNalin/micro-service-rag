"""Typed settings shared by all services, loaded from environment variables."""

import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    """Fields every service needs; each reads the env var of the same name in upper case."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    service_name: str = "service"
    environment: str = "development"
    log_level: str = "INFO"
    internal_api_token: str
    jwt_issuer: str = "docmind-auth"
    jwt_audience: str = "docmind-api"
    auth_jwks_url: str = "http://auth:8003/.well-known/jwks.json"


class VectorSettings(BaseServiceSettings):
    """Settings for services that use Qdrant and the embedding models (ingestion, retrieval)."""

    qdrant_url: str = "http://qdrant:6333"
    chunks_collection: str = "documents"
    cache_collection: str = "semantic_cache"
    dense_model: str = "BAAI/bge-small-en-v1.5"
    sparse_model: str = "Qdrant/bm25"
    model_cache: str = "/models"
    per_tenant_idf: bool = True


@lru_cache
def get_base_settings() -> BaseServiceSettings:
    """Return the cached base settings used by common.security and common.logging."""
    return BaseServiceSettings()


@lru_cache
def get_vector_settings() -> VectorSettings:
    """Return the cached vector settings used by common.embeddings and common.vectorstore."""
    return VectorSettings()


_SECRET_FIELD = re.compile(r"token|password|secret|api_key|database_url", re.I)


def check_production_secrets(settings: BaseSettings) -> None:
    """Refuse to start in production while any secret setting still holds a `change-me` placeholder."""
    if getattr(settings, "environment", "development").lower() != "production":
        return
    bad = sorted(
        name
        for name, value in settings.model_dump().items()
        if _SECRET_FIELD.search(name) and isinstance(value, str) and "change-me" in value
    )
    if bad:
        raise RuntimeError(f"refusing to start in production with placeholder secrets: {', '.join(bad)}")


def clear_settings_caches() -> None:
    """Clear both settings caches (tests call this after changing env vars)."""
    get_base_settings.cache_clear()
    get_vector_settings.cache_clear()
