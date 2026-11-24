"""Step 2.2: password hashing and rules, signing keys, JWKS and access-token creation."""

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from auth_service import config as auth_config
from auth_service import keys
from auth_service.models import User, UserRole
from auth_service.passwords import (
    hash_password,
    validate_password_strength,
    verify_dummy,
    verify_password,
)
from auth_service.tokens import create_access_token, generate_refresh_token, hash_token
from common.security import JWTVerifier
from tests.conftest import TEST_AUDIENCE, TEST_ISSUER


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    key_path = tmp_path / "keys" / "jwt_private.pem"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///unused.db")
    monkeypatch.setenv("JWT_PRIVATE_KEY_PATH", str(key_path))
    auth_config.get_settings.cache_clear()
    saved = dict(keys._state)
    keys._state.clear()
    yield key_path
    keys._state.clear()
    keys._state.update(saved)
    auth_config.get_settings.cache_clear()


# ---------- passwords ----------

def test_hash_and_verify_round_trip() -> None:
    hashed = hash_password("correct-horse-42")
    assert hashed.startswith("$argon2id$")
    assert "correct-horse-42" not in hashed
    assert verify_password("correct-horse-42", hashed) == (True, None)
    assert verify_password("wrong-horse-42", hashed)[0] is False


def test_hashes_are_salted() -> None:
    assert hash_password("same-password-1") != hash_password("same-password-1")


def test_verify_against_garbage_hash_is_false() -> None:
    assert verify_password("anything-1", "not-a-real-hash") == (False, None)


def test_verify_dummy_runs() -> None:
    verify_dummy("whatever-123")  # must not raise


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short1", "at least 10"),
        ("onlyletters-here", "digit"),
        ("1234567890123", "letter"),
        ("alice-2026-secret", "email"),
        ("x" * 256 + "1", "at most"),
    ],
    ids=["too-short", "no-digit", "no-letter", "contains-email", "too-long"],
)
def test_weak_passwords_are_rejected(password: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_password_strength(password, email="Alice@example.com", min_length=10)


def test_strong_password_is_accepted() -> None:
    validate_password_strength("blue-river-7-rocks", email="alice@example.com", min_length=10)


def test_short_email_local_part_is_not_a_rule() -> None:
    # "al" is too short to be a meaningful match, so it must not block passwords containing it
    validate_password_strength("totally-valid-pw-9", email="al@example.com", min_length=10)


# ---------- keys ----------

def test_load_or_create_creates_then_reloads_same_key(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "jwt.pem"
    first = keys.load_or_create_private_key(str(path))
    assert path.exists()
    assert path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")  # unencrypted PKCS8
    second = keys.load_or_create_private_key(str(path))
    assert first.private_numbers() == second.private_numbers()
    assert first.key_size == 2048
    assert first.public_key().public_numbers().e == 65537


def _b64_to_int(value: str) -> int:
    return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")


def test_key_id_matches_rfc7638_example() -> None:
    # RFC 7638 section 3.1 example key and its published thumbprint
    n = (
        "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECP"
        "ebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_FDW2QvzqY"
        "368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0f"
        "M4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
    )
    public = RSAPublicNumbers(e=_b64_to_int("AQAB"), n=_b64_to_int(n)).public_key()
    assert keys.key_id(public) == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"


def test_accessors_require_init(auth_settings: Path) -> None:
    for accessor in (keys.signing_key, keys.public_key, keys.current_kid, keys.jwks):
        with pytest.raises(RuntimeError):
            accessor()


def test_jwks_shape(auth_settings: Path) -> None:
    keys.init_keys(str(auth_settings))
    doc = keys.jwks()
    assert len(doc["keys"]) == 1
    jwk = doc["keys"][0]
    assert {"kty", "n", "e", "kid", "use", "alg"} <= set(jwk)
    assert "key_ops" not in jwk
    assert (jwk["kty"], jwk["use"], jwk["alg"], jwk["kid"]) == ("RSA", "sig", "RS256", keys.current_kid())
    assert not {"d", "p", "q", "dp", "dq", "qi"} & set(jwk), "private key material must never be published"


def test_kid_is_stable_across_restarts(auth_settings: Path) -> None:
    keys.init_keys(str(auth_settings))
    kid = keys.current_kid()
    keys._state.clear()
    keys.init_keys(str(auth_settings))  # simulated restart: same file, same key
    assert keys.current_kid() == kid


# ---------- tokens ----------

def _user(role: UserRole = UserRole.admin) -> User:
    return User(id=uuid.uuid4(), tenant_id=uuid.uuid4(), email="boss@acme.io", full_name="Boss", role=role)


def test_access_token_decodes_into_principal(auth_settings: Path) -> None:
    keys.init_keys(str(auth_settings))
    user = _user(UserRole.member)
    token, ttl = create_access_token(user)
    assert ttl == 15 * 60

    verifier = JWTVerifier(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, public_key=keys.public_key())
    p = verifier.verify(token)
    expected = (str(user.id), str(user.tenant_id), "member", user.email)
    assert (p.user_id, p.tenant_id, p.role, p.email) == expected

    header = jwt.get_unverified_header(token)
    assert header == {"alg": "RS256", "typ": "JWT", "kid": keys.current_kid()}
    claims = jwt.decode(token, options={"verify_signature": False})
    assert set(claims) == {"iss", "aud", "sub", "tid", "role", "email", "iat", "nbf", "exp", "jti", "type"}
    assert claims["exp"] - claims["iat"] == ttl and claims["nbf"] == claims["iat"]
    assert claims["type"] == "access"


def test_access_token_respects_now_and_ttl_setting(
    auth_settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MIN", "5")
    auth_config.get_settings.cache_clear()
    keys.init_keys(str(auth_settings))
    fixed = datetime(2030, 1, 1, tzinfo=UTC)
    token, ttl = create_access_token(_user(), now=fixed)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert ttl == 300 and claims["iat"] == int(fixed.timestamp()) and claims["exp"] == claims["iat"] + 300


def test_each_access_token_has_unique_jti(auth_settings: Path) -> None:
    keys.init_keys(str(auth_settings))
    user = _user()
    tokens = [create_access_token(user)[0] for _ in range(5)]
    jtis = {jwt.decode(t, options={"verify_signature": False})["jti"] for t in tokens}
    assert len(jtis) == 5


def test_refresh_tokens_are_random_and_hashed() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b and len(a) >= 64
    assert hash_token(a) == hash_token(a)
    assert len(hash_token(a)) == 64 and int(hash_token(a), 16) >= 0
    assert hash_token(a) != hash_token(b)
