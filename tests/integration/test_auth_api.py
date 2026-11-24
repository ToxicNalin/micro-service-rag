"""Auth service API. Step 2.1: health, error handling, schema and migrations; later steps extend this."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
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


# ---------- Step 2.2: JWKS endpoint ----------

def test_jwks_endpoint(auth_client: TestClient) -> None:
    resp = auth_client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=300"
    (jwk,) = resp.json()["keys"]
    assert jwk["kty"] == "RSA" and jwk["alg"] == "RS256" and jwk["use"] == "sig" and jwk["kid"]
    assert "d" not in jwk and "key_ops" not in jwk


# ---------- Step 2.3: register / login / refresh / logout / me / change password ----------

PASSWORD = "river-stone-2048"
NEW_PASSWORD = "mountain-lake-4096"


def _register(
    client: TestClient, email: str = "Alice@Acme.io", tenant: str = "Acme Corp", password: str = PASSWORD
) -> dict:
    resp = client.post(
        "/auth/register",
        json={"tenant_name": tenant, "full_name": "Alice Admin", "email": email, "password": password},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login(client: TestClient, email: str = "alice@acme.io", password: str = PASSWORD) -> httpx.Response:
    return client.post("/auth/login", json={"email": email, "password": password})


def _refresh(client: TestClient, raw: str) -> httpx.Response:
    return client.post("/auth/refresh", json={"refresh_token": raw})


def _me(client: TestClient, access: str) -> httpx.Response:
    return client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})


def test_register_returns_tokens_and_admin(auth_client: TestClient) -> None:
    body = _register(auth_client)
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900 and body["refresh_expires_in"] == 7 * 86400
    assert body["access_token"] and body["refresh_token"]

    me = _me(auth_client, body["access_token"])
    assert me.status_code == 200
    data = me.json()
    assert data["user"]["role"] == "admin"
    assert data["user"]["email"] == "alice@acme.io"  # normalized to lower case
    assert data["tenant"]["name"] == "Acme Corp" and data["tenant"]["slug"] == "acme-corp"
    assert "password_hash" not in me.text


def test_register_duplicate_email_is_409_case_insensitive(auth_client: TestClient) -> None:
    _register(auth_client)
    resp = auth_client.post(
        "/auth/register",
        json={"tenant_name": "Other", "full_name": "Eve", "email": "ALICE@acme.IO", "password": PASSWORD},
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "email already registered"}


def test_register_same_org_name_gets_unique_slug(auth_client: TestClient) -> None:
    a = _register(auth_client, email="a@acme.io")
    b = _register(auth_client, email="b@acme.io")
    slug_a = _me(auth_client, a["access_token"]).json()["tenant"]["slug"]
    slug_b = _me(auth_client, b["access_token"]).json()["tenant"]["slug"]
    assert (slug_a, slug_b) == ("acme-corp", "acme-corp-2")


@pytest.mark.parametrize("password", ["short1", "no-digits-at-all", "alice-12345678"])
def test_register_weak_password_is_400(auth_client: TestClient, password: str) -> None:
    resp = auth_client.post(
        "/auth/register",
        json={"tenant_name": "Acme", "full_name": "Alice", "email": "alice@acme.io", "password": password},
    )
    assert resp.status_code == 400


def test_register_rejects_extra_fields(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/auth/register",
        json={
            "tenant_name": "Acme", "full_name": "A", "email": "a@acme.io", "password": PASSWORD,
            "tenant_id": str(uuid.uuid4()), "role": "admin",
        },
    )
    assert resp.status_code == 422


def test_register_disabled_is_403(auth_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "false")
    auth_config.get_settings.cache_clear()
    with TestClient(create_app()) as client:
        resp = client.post(
            "/auth/register",
            json={"tenant_name": "Acme", "full_name": "A", "email": "a@acme.io", "password": PASSWORD},
        )
    assert resp.status_code == 403


def test_login_success_and_wrong_password(auth_client: TestClient) -> None:
    _register(auth_client)
    ok = _login(auth_client, email="  ALICE@acme.io ")
    assert ok.status_code == 200 and ok.json()["access_token"]
    bad = _login(auth_client, password="wrong-password-1")
    assert bad.status_code == 401 and bad.json() == {"detail": "invalid email or password"}


def test_login_unknown_email_looks_like_wrong_password(auth_client: TestClient) -> None:
    resp = _login(auth_client, email="nobody@acme.io")
    assert resp.status_code == 401 and resp.json() == {"detail": "invalid email or password"}


def test_lockout_after_five_failures(auth_client: TestClient) -> None:
    _register(auth_client)
    for _ in range(5):
        assert _login(auth_client, password="wrong-password-1").status_code == 401
    locked = _login(auth_client)  # even the right password is refused while locked
    assert locked.status_code == 423
    assert locked.json() == {"detail": "account locked, try again later"}


def test_successful_login_resets_failure_count(auth_client: TestClient) -> None:
    _register(auth_client)
    for _ in range(4):
        _login(auth_client, password="wrong-password-1")
    assert _login(auth_client).status_code == 200
    for _ in range(4):
        _login(auth_client, password="wrong-password-1")
    assert _login(auth_client).status_code == 200  # counter was reset, so still not locked


def test_refresh_rotates_and_old_token_fails(auth_client: TestClient) -> None:
    first = _register(auth_client)
    resp = _refresh(auth_client, first["refresh_token"])
    assert resp.status_code == 200
    second = resp.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert _me(auth_client, second["access_token"]).status_code == 200

    reused = _refresh(auth_client, first["refresh_token"])
    assert reused.status_code == 401
    assert reused.json() == {"detail": "refresh token reuse detected"}


def test_reuse_revokes_whole_family(auth_client: TestClient) -> None:
    first = _register(auth_client)
    second = _refresh(auth_client, first["refresh_token"]).json()
    third = _refresh(auth_client, second["refresh_token"]).json()

    assert _refresh(auth_client, first["refresh_token"]).status_code == 401  # theft detected
    newest = _refresh(auth_client, third["refresh_token"])
    assert newest.status_code == 401, "the newest token of the family must be revoked too"


def test_reuse_does_not_touch_other_logins(auth_client: TestClient) -> None:
    first = _register(auth_client)
    other_login = _login(auth_client).json()  # a separate family (e.g. another device)
    _refresh(auth_client, first["refresh_token"])
    _refresh(auth_client, first["refresh_token"])  # reuse in family 1
    assert _refresh(auth_client, other_login["refresh_token"]).status_code == 200


def test_unknown_refresh_token_is_401(auth_client: TestClient) -> None:
    resp = _refresh(auth_client, "made-up-token")
    assert resp.status_code == 401 and resp.json() == {"detail": "invalid refresh token"}


def test_expired_refresh_token_is_401(auth_client: TestClient, auth_env: str) -> None:
    raw = _register(auth_client)["refresh_token"]
    engine = sa.create_engine(_sync_url(auth_env))
    with engine.begin() as conn:
        conn.execute(sa.update(RefreshToken).values(expires_at=sa.func.datetime("now", "-1 minute")))
    engine.dispose()
    resp = _refresh(auth_client, raw)
    assert resp.status_code == 401 and resp.json() == {"detail": "refresh token expired"}


def test_refresh_tokens_stored_only_as_hashes(auth_client: TestClient, auth_env: str) -> None:
    raw = _register(auth_client)["refresh_token"]
    engine = sa.create_engine(_sync_url(auth_env))
    with engine.connect() as conn:
        stored = conn.execute(sa.select(RefreshToken.token_hash)).scalars().all()
        password_hash = conn.execute(sa.select(User.password_hash)).scalar_one()
    engine.dispose()
    assert raw not in stored and len(stored) == 1 and len(stored[0]) == 64
    assert password_hash.startswith("$argon2id$") and PASSWORD not in password_hash


def test_logout_revokes_refresh_token(auth_client: TestClient) -> None:
    raw = _register(auth_client)["refresh_token"]
    resp = auth_client.post("/auth/logout", json={"refresh_token": raw})
    assert resp.status_code == 204
    assert _refresh(auth_client, raw).status_code == 401
    # idempotent, and unknown tokens are ignored
    assert auth_client.post("/auth/logout", json={"refresh_token": raw}).status_code == 204
    assert auth_client.post("/auth/logout", json={"refresh_token": "unknown"}).status_code == 204


def test_me_requires_token(auth_client: TestClient) -> None:
    assert auth_client.get("/auth/me").status_code == 401
    assert _me(auth_client, "not.a.token").status_code == 401


def test_me_rejects_refresh_token_as_bearer(auth_client: TestClient) -> None:
    raw = _register(auth_client)["refresh_token"]
    assert _me(auth_client, raw).status_code == 401


def test_change_password_revokes_all_refresh_tokens(auth_client: TestClient) -> None:
    reg = _register(auth_client)
    other = _login(auth_client).json()
    headers = {"Authorization": f"Bearer {reg['access_token']}"}

    wrong = auth_client.post(
        "/auth/change-password",
        json={"current_password": "not-my-password-1", "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert wrong.status_code == 400
    weak = auth_client.post(
        "/auth/change-password", json={"current_password": PASSWORD, "new_password": "weak"}, headers=headers
    )
    assert weak.status_code == 400

    ok = auth_client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert ok.status_code == 204
    assert _refresh(auth_client, reg["refresh_token"]).status_code == 401
    assert _refresh(auth_client, other["refresh_token"]).status_code == 401
    assert _login(auth_client).status_code == 401
    assert _login(auth_client, password=NEW_PASSWORD).status_code == 200


def test_change_password_requires_token(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 401


def test_login_stores_client_metadata(auth_client: TestClient, auth_env: str) -> None:
    _register(auth_client)
    auth_client.post(
        "/auth/login",
        json={"email": "alice@acme.io", "password": PASSWORD},
        headers={"User-Agent": "pytest-agent", "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
    )
    engine = sa.create_engine(_sync_url(auth_env))
    with engine.connect() as conn:
        rows = conn.execute(sa.select(RefreshToken.user_agent, RefreshToken.ip_address)).all()
    engine.dispose()
    assert ("pytest-agent", "203.0.113.9") in rows
