"""Auth service app: tenants, users, refresh tokens and the JWT signing key."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from auth_service import keys
from auth_service.config import get_settings
from auth_service.db import ping_db
from auth_service.routers import auth, jwks, users
from auth_service.service import ServiceError
from common.logging import RequestIdMiddleware, configure_logging, get_request_id
from common.security import AuthError, JWTVerifier, set_verifier
from common.settings import check_production_secrets

log = logging.getLogger("auth_service")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup checks, then load/create the signing key and verify tokens with our own public key."""
    settings = get_settings()
    check_production_secrets(settings)
    keys.init_keys(settings.jwt_private_key_path)
    set_verifier(
        JWTVerifier(issuer=settings.jwt_issuer, audience=settings.jwt_audience, public_key=keys.public_key())
    )
    log.info(
        "auth service starting",
        extra={"environment": settings.environment, "kid": keys.current_kid()},
    )
    yield
    log.info("auth service stopping")


async def _service_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ServiceError)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


async def _auth_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": str(exc) or "invalid token"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


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
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(AuthError, _auth_error)
    app.add_exception_handler(Exception, _unhandled_error)
    app.include_router(jwks.router)
    app.include_router(auth.router)
    app.include_router(users.router)

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
