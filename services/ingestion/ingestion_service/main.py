"""Ingestion service app: tenant-scoped uploads -> parse -> chunk -> embed -> Qdrant."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common import embeddings, vectorstore
from common.logging import RequestIdMiddleware, configure_logging, unhandled_exception_handler
from common.settings import check_production_secrets
from ingestion_service.config import get_settings
from ingestion_service.db import ping_db

log = logging.getLogger("ingestion_service")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wait for Qdrant, load the embedding models, and make sure the chunks collection exists."""
    settings = get_settings()
    check_production_secrets(settings)
    await anyio.to_thread.run_sync(vectorstore.wait_for_qdrant)
    log.info("loading embedding models (first start downloads them)")
    await anyio.to_thread.run_sync(embeddings.warmup)
    dim = await anyio.to_thread.run_sync(embeddings.dense_dim)
    await anyio.to_thread.run_sync(vectorstore.ensure_chunks_collection, dim)
    log.info(
        "ingestion service starting",
        extra={
            "environment": settings.environment,
            "dense_dim": dim,
            "collection": settings.chunks_collection,
        },
    )
    yield
    log.info("ingestion service stopping")


def _qdrant_ok() -> bool:
    try:
        vectorstore.client().get_collections()
        return True
    except Exception:
        log.warning("qdrant ping failed", exc_info=True)
        return False


def create_app() -> FastAPI:
    """Build the ingestion FastAPI app."""
    # read straight from the environment: settings validation must not run at import time
    configure_logging(os.getenv("SERVICE_NAME", "ingestion"), os.getenv("LOG_LEVEL", "INFO"))
    app = FastAPI(title="DocMind ingestion", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/health")
    async def health() -> JSONResponse:
        """Liveness plus database and Qdrant reachability; 503 if either is down."""
        db_ok = await ping_db()
        qdrant_ok = await anyio.to_thread.run_sync(_qdrant_ok)
        return JSONResponse(
            {"status": "ok", "db": "ok" if db_ok else "down", "qdrant": "ok" if qdrant_ok else "down"},
            status_code=200 if db_ok and qdrant_ok else 503,
        )

    return app


app = create_app()
