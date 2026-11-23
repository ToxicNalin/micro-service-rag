"""Step 1.2: JWT verification, role checks and the internal token."""

import logging
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from common.logging import RequestIdMiddleware, request_id_var
from common.security import (
    AuthError,
    JWTVerifier,
    Principal,
    forward_headers,
    get_principal,
    get_verifier,
    internal_headers,
    require_admin,
    require_internal_token,
    require_roles,
    set_verifier,
)
from tests.conftest import TEST_AUDIENCE, TEST_INTERNAL_TOKEN, TEST_ISSUER, TEST_KID

require_any_role = require_roles("admin", "member")


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/me")
    async def me(p: Principal = Depends(get_principal)) -> dict[str, Any]:
        return {"user_id": p.user_id, "tenant_id": p.tenant_id, "role": p.role, "email": p.email}

    @app.get("/admin")
    async def admin(p: Principal = Depends(require_admin)) -> dict[str, str]:
        return {"ok": p.user_id}

    @app.get("/members-or-admins")
    async def both(p: Principal = Depends(require_any_role)) -> dict[str, str]:
        return {"role": p.role}

    @app.post("/internal/ping", dependencies=[Depends(require_internal_token)])
    async def internal() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def client(use_local_verifier: JWTVerifier) -> TestClient:
    return TestClient(_app())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------- valid tokens ----------

def test_valid_token_gives_principal(client: TestClient, make_token: Callable[..., str]) -> None:
    resp = client.get("/me", headers=_bearer(make_token("t-1", "u-1", "member", "m@x.io")))
    assert resp.status_code == 200
    assert resp.json() == {"user_id": "u-1", "tenant_id": "t-1", "role": "member", "email": "m@x.io"}


def test_verify_returns_principal_with_raw_token(
    use_local_verifier: JWTVerifier, make_token: Callable[..., str]
) -> None:
    token = make_token("t-9", "u-9", "admin")
    p = use_local_verifier.verify(token)
    assert (p.tenant_id, p.user_id, p.role, p.is_admin, p.token) == ("t-9", "u-9", "admin", True, token)
    assert token not in repr(p)


# ---------- rejected tokens -> 401 ----------

@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"expired": True}, "token expired"),
        ({"audience": "someone-else"}, "invalid token"),
        ({"issuer": "evil-issuer"}, "invalid token"),
        ({"typ": "refresh"}, "invalid token"),
        ({"omit": ("tid",)}, "invalid token"),
        ({"tid": ""}, "invalid token"),
        ({"role": "superuser"}, "invalid token"),
        ({"omit": ("jti",)}, "invalid token"),
        ({"omit": ("exp",)}, "invalid token"),
        ({"omit": ("sub",)}, "invalid token"),
    ],
    ids=["expired", "wrong-aud", "wrong-iss", "refresh-type", "missing-tid", "empty-tid",
         "bad-role", "missing-jti", "missing-exp", "missing-sub"],
)
def test_bad_claims_give_401(
    client: TestClient, make_token: Callable[..., str], overrides: dict[str, Any], detail: str
) -> None:
    tid = overrides.pop("tid", "tenant-a")
    role = overrides.pop("role", "admin")
    resp = client.get("/me", headers=_bearer(make_token(tid, "user-1", role, **overrides)))
    assert resp.status_code == 401
    assert resp.json()["detail"] == detail
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_bad_signature_gives_401(client: TestClient, make_token: Callable[..., str]) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    resp = client.get("/me", headers=_bearer(make_token(key=other_key)))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


def test_hs256_token_is_rejected(client: TestClient) -> None:
    forged = jwt.encode(
        {"sub": "u", "tid": "t", "role": "admin", "type": "access", "iss": TEST_ISSUER,
         "aud": TEST_AUDIENCE, "iat": 1, "exp": 9999999999, "jti": "x"},
        "a-shared-secret-of-at-least-32-bytes!!",
        algorithm="HS256",
    )
    assert client.get("/me", headers=_bearer(forged)).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not.a.jwt"},
    ],
    ids=["missing", "empty-bearer", "basic-scheme", "garbage"],
)
def test_missing_or_malformed_header_gives_401(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.get("/me", headers=headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_get_principal_never_logs_token(
    client: TestClient, make_token: Callable[..., str], caplog: pytest.LogCaptureFixture
) -> None:
    good, expired = make_token(), make_token(expired=True)
    with caplog.at_level(logging.DEBUG):
        client.get("/me", headers=_bearer(good))
        client.get("/me", headers=_bearer(expired))
    assert good not in caplog.text and expired not in caplog.text


# ---------- roles ----------

def test_require_admin_rejects_member(client: TestClient, make_token: Callable[..., str]) -> None:
    resp = client.get("/admin", headers=_bearer(make_token(role="member")))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient role"


def test_require_admin_accepts_admin(client: TestClient, make_token: Callable[..., str]) -> None:
    assert client.get("/admin", headers=_bearer(make_token(role="admin"))).status_code == 200


def test_require_admin_without_token_is_401(client: TestClient) -> None:
    assert client.get("/admin").status_code == 401


def test_require_roles_allows_listed_roles(client: TestClient, make_token: Callable[..., str]) -> None:
    for role in ("admin", "member"):
        resp = client.get("/members-or-admins", headers=_bearer(make_token(role=role)))
        assert resp.json() == {"role": role}


# ---------- internal token ----------

def test_internal_route_without_token_is_403(client: TestClient) -> None:
    assert client.post("/internal/ping").status_code == 403


def test_internal_route_with_wrong_token_is_403(client: TestClient) -> None:
    assert client.post("/internal/ping", headers={"X-Internal-Token": "nope"}).status_code == 403


def test_internal_route_with_right_token_is_200(client: TestClient) -> None:
    resp = client.post("/internal/ping", headers={"X-Internal-Token": TEST_INTERNAL_TOKEN})
    assert resp.status_code == 200


def test_user_jwt_does_not_open_internal_route(client: TestClient, make_token: Callable[..., str]) -> None:
    assert client.post("/internal/ping", headers=_bearer(make_token())).status_code == 403


# ---------- helpers and verifier wiring ----------

def test_internal_and_forward_headers(principal_factory: Callable[..., Principal]) -> None:
    p = principal_factory()
    token = request_id_var.set("rid-42")
    try:
        assert internal_headers() == {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Request-ID": "rid-42"}
        assert forward_headers(p) == {"Authorization": f"Bearer {p.token}", "X-Request-ID": "rid-42"}
    finally:
        request_id_var.reset(token)


def test_verifier_needs_exactly_one_key_source(rsa_key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(ValueError):
        JWTVerifier(issuer="i", audience="a")
    with pytest.raises(ValueError):
        JWTVerifier(issuer="i", audience="a", jwks_url="http://x", public_key=rsa_key.public_key())


def test_get_verifier_is_lazy_jwks_singleton() -> None:
    v = get_verifier()
    assert v is get_verifier()
    assert v.jwks_client is not None
    assert v.jwks_client.uri == "http://auth.invalid/.well-known/jwks.json"
    assert (v.issuer, v.audience) == (TEST_ISSUER, TEST_AUDIENCE)


def test_jwks_mode_picks_key_by_kid(
    monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey, make_token: Callable[..., str]
) -> None:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key(), as_dict=True)
    jwk.update(kid=TEST_KID, use="sig", alg="RS256")
    calls = {"n": 0}

    def fake_fetch(self: jwt.PyJWKClient) -> dict[str, Any]:
        calls["n"] += 1
        return {"keys": [jwk]}

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fake_fetch)
    verifier = JWTVerifier(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, jwks_url="http://auth.invalid/jwks")
    set_verifier(verifier)

    assert verifier.verify(make_token("t-j", "u-j")).tenant_id == "t-j"
    assert verifier.verify(make_token("t-j", "u-k")).user_id == "u-k"
    assert calls["n"] == 1  # the key set is cached
    with pytest.raises(AuthError):
        verifier.verify(make_token(kid="unknown-kid"))
