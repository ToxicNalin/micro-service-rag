"""Async database engine and sessions, created lazily (never at import time)."""

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from auth_service.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for the auth ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, creating it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield a session and close it afterwards."""
    async with get_sessionmaker()() as session:
        yield session


async def ping_db() -> bool:
    """Run SELECT 1; return False on any error."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        log.warning("database ping failed", exc_info=True)
        return False


def reset_engine() -> None:
    """Drop the cached engine and session factory (tests, after changing DATABASE_URL)."""
    global _engine, _sessionmaker
    if _engine is not None:
        # close=False: just forget pooled connections; closing them needs the engine's event loop
        _engine.sync_engine.dispose(close=False)
    _engine = None
    _sessionmaker = None
