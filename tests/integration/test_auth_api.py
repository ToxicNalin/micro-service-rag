"""Auth service API. Step 2.1: health, error handling, schema and migrations; later steps extend this."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_service import config as auth_config
from auth_service import db as auth_db
from auth_service.main import create_app
from auth_service.models import Base, RefreshToken, Tenant, User, UserRole

AUTH_DIR = Path(__file__).resolve().parents[2] / "services" / "auth"


def _sync_url(async_url: str) -> str:
    return async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def _use_database(monkeypatch: pytest.MonkeyPatch, url: str, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(tmp_path / "jwt_private.pem"))
    auth_config.get_settings.cache_clear()
    auth_db.reset_engine()


@pytest.fixture
def auth_env(monkeypatch: pytest.MonkeyPatch, sqlite_url: str, tmp_path: Path) -> Iterator[str]:
    """Point the auth service at a fresh SQLite database with all tables created."""
    _use_database(monkeypatch, sqlite_url, tmp_path)
    engine = sa.create_engine(_sync_url(sqlite_url))
    Base.metadata.create_all(engine)
    engine.dispose()
    yield sqlite_url
    auth_db.reset_engine()
    auth_config.get_settings.cache_clear()


@pytest.fixture
def auth_client(auth_env: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


# ---------- health ----------

def test_health(auth_client: TestClient) -> None:
    resp = auth_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}
    assert len(resp.headers["X-Request-ID"]) == 32


def test_health_reports_db_down(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = (tmp_path / "no-such-dir" / "x.db").as_posix()
    _use_database(monkeypatch, f"sqlite+aiosqlite:///{missing}", tmp_path)
    with TestClient(create_app()) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"] == "down"
    auth_db.reset_engine()
    auth_config.get_settings.cache_clear()


def test_unhandled_error_is_generic(auth_env: str) -> None:
    app: FastAPI = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals: password=hunter2")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom", headers={"X-Request-ID": "req-boom-1"})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "internal error", "request_id": "req-boom-1"}
    assert resp.headers["X-Request-ID"] == "req-boom-1"
    assert "hunter2" not in resp.text and "Traceback" not in resp.text


def test_production_refuses_placeholder_secrets(monkeypatch: pytest.MonkeyPatch, auth_env: str) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "change-me-to-a-long-random-string")
    auth_config.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="internal_api_token"), TestClient(create_app()):
        pass


# ---------- schema (portable models on SQLite) ----------

def test_models_apply_defaults(auth_env: str) -> None:
    engine = sa.create_engine(_sync_url(auth_env))
    with sa.orm.Session(engine) as s:
        tenant = Tenant(name="Acme", slug="acme")
        s.add(tenant)
        s.flush()
        user = User(tenant_id=tenant.id, email="a@acme.io", full_name="A", password_hash="x")
        s.add(user)
        s.flush()
        s.add(
            RefreshToken(
                user_id=user.id, family_id=uuid.uuid4(), token_hash="0" * 64,
                expires_at=sa.func.now(),
            )
        )
        s.commit()
        loaded = s.scalars(sa.select(User)).one()
        assert loaded.role == UserRole.member
        assert loaded.is_active is True and loaded.failed_login_count == 0
        assert loaded.tenant.slug == "acme"
        assert loaded.created_at is not None
        assert "password" not in repr(loaded)
    engine.dispose()


def test_email_and_slug_are_unique(auth_env: str) -> None:
    engine = sa.create_engine(_sync_url(auth_env))
    with sa.orm.Session(engine) as s:
        t = Tenant(name="A", slug="a")
        s.add(t)
        s.flush()
        s.add_all([
            User(tenant_id=t.id, email="dup@x.io", full_name="1", password_hash="h"),
            User(tenant_id=t.id, email="dup@x.io", full_name="2", password_hash="h"),
        ])
        with pytest.raises(sa.exc.IntegrityError):
            s.commit()
    engine.dispose()


# ---------- migrations ----------

def _alembic_config(url: str) -> Config:
    cfg = Config(str(AUTH_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(AUTH_DIR / "migrations"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


def test_migration_matches_models(monkeypatch: pytest.MonkeyPatch, sqlite_url: str, tmp_path: Path) -> None:
    """`alembic upgrade head` must produce exactly the schema the ORM models describe."""
    _use_database(monkeypatch, sqlite_url, tmp_path)
    cfg = _alembic_config(sqlite_url)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(_sync_url(sqlite_url))
    with engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        assert {"tenants", "users", "refresh_tokens", "alembic_version"} <= tables
        indexes = {i["name"] for t in ("users", "refresh_tokens") for i in sa.inspect(conn).get_indexes(t)}
        assert {"ix_users_tenant_id", "ix_refresh_tokens_user_id", "ix_refresh_tokens_family_id"} <= indexes
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migration differ: {diff}"

    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert set(sa.inspect(conn).get_table_names()) == {"alembic_version"}
    engine.dispose()
    auth_config.get_settings.cache_clear()
