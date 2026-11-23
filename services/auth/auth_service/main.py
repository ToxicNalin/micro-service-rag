"""Auth service app: tenants, users, refresh tokens and (from Step 2.2) JWT signing keys."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from auth_service.config import get_settings
from auth_service.db import ping_db
from common.logging import RequestIdMiddleware, configure_logging, get_request_id
from common.settings import check_production_secrets

log = logging.getLogger("auth_service")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup checks; signing keys are added in Step 2.2."""
    settings = get_settings()
    check_production_secrets(settings)
    log.info("auth service starting", extra={"environment": settings.environment})
    yield
    log.info("auth service stopping")


async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or get_request_id()
    log.error(
        "unhandled error",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"path": request.url.path, "request_id": request_id},
    )
    return JSONResponse(
        {"detail": "internal error", "request_id": request_id},
        status_code=500,
        headers={"X-Request-ID": request_id},
    )


def create_app() -> FastAPI:
    """Build the auth FastAPI app."""
    # read straight from the environment: settings validation must not run at import time
    configure_logging(os.getenv("SERVICE_NAME", "auth"), os.getenv("LOG_LEVEL", "INFO"))
    app = FastAPI(title="DocMind auth", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(Exception, _unhandled_error)

    @app.get("/health")
    async def health() -> JSONResponse:
        """Liveness plus database reachability; 503 if the database is down."""
        db_ok = await ping_db()
        return JSONResponse(
            {"status": "ok", "db": "ok" if db_ok else "down"},
            status_code=200 if db_ok else 503,
        )

    return app


app = create_app()
