"""Ingestion service settings."""

from functools import lru_cache

from common.settings import VectorSettings


class Settings(VectorSettings):
    """Settings for the ingestion service, read from environment variables."""

    database_url: str
    chunk_size: int = 900
    chunk_overlap: int = 150
    max_upload_mb: int = 25
    max_documents_per_tenant: int = 200
    ingestion_concurrency: int = 2
    retrieval_url: str = "http://retrieval:8002"


@lru_cache
def get_settings() -> Settings:
    """Return the cached ingestion settings."""
    return Settings()
