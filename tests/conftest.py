"""Shared pytest fixtures. Later steps extend this file."""

import re
import time
import uuid
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from qdrant_client import QdrantClient

from common import embeddings, vectorstore
from common.security import JWTVerifier, Principal, set_verifier
from common.settings import clear_settings_caches

TEST_INTERNAL_TOKEN = "test-internal-token"
TEST_ISSUER = "docmind-auth"
TEST_AUDIENCE = "docmind-api"


@pytest.fixture(autouse=True)
def test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a known, offline configuration and fresh settings caches."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SERVICE_NAME", "test")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("INTERNAL_API_TOKEN", TEST_INTERNAL_TOKEN)
    monkeypatch.setenv("JWT_ISSUER", TEST_ISSUER)
    monkeypatch.setenv("JWT_AUDIENCE", TEST_AUDIENCE)
    monkeypatch.setenv("AUTH_JWKS_URL", "http://auth.invalid/.well-known/jwks.json")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("MODEL_CACHE", "/tmp/docmind-test-models")  # noqa: S108 - never written by tests
    monkeypatch.setenv("PER_TENANT_IDF", "true")
    clear_settings_caches()
    set_verifier(None)

    # Tests must never download or load real models: fail loudly unless `fake_embeddings` is used.
    def _no_real_models() -> None:
        raise RuntimeError("real embedding models are disabled in tests; use the fake_embeddings fixture")

    monkeypatch.setattr(embeddings, "_dense", _no_real_models)
    monkeypatch.setattr(embeddings, "_sparse", _no_real_models)
    yield
    set_verifier(None)
    clear_settings_caches()


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    """Async SQLite URL in a per-test temp folder (DB tests run without Postgres)."""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


# ---------- JWT (Step 1.2) ----------

TEST_KID = "test-kid"


@pytest.fixture(scope="session")
def rsa_key() -> RSAPrivateKey:
    """One RSA-2048 signing key for the whole test session."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


MakeToken = Callable[..., str]


@pytest.fixture
def make_token(rsa_key: RSAPrivateKey) -> MakeToken:
    """Sign access tokens with the test key using the §7.4 claims; keyword overrides break them on purpose."""

    def _make(
        tenant_id: str = "tenant-a",
        user_id: str = "user-1",
        role: str = "admin",
        email: str = "admin@a.example",
        *,
        expired: bool = False,
        typ: str = "access",
        issuer: str = TEST_ISSUER,
        audience: str = TEST_AUDIENCE,
        ttl_s: int = 900,
        key: RSAPrivateKey | None = None,
        kid: str = TEST_KID,
        omit: tuple[str, ...] = (),
        **extra: Any,
    ) -> str:
        now = int(time.time())
        iat = now - 3600 if expired else now
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "sub": user_id,
            "tid": tenant_id,
            "role": role,
            "email": email,
            "iat": iat,
            "nbf": iat,
            "exp": iat + (60 if expired else ttl_s),
            "jti": uuid.uuid4().hex,
            "type": typ,
            **extra,
        }
        for name in omit:
            claims.pop(name, None)
        return jwt.encode(claims, key or rsa_key, algorithm="RS256", headers={"kid": kid})

    return _make


@pytest.fixture
def principal_factory(make_token: MakeToken) -> Callable[..., Principal]:
    """Build a Principal backed by a real, valid access token."""

    def _make(
        tenant_id: str = "tenant-a",
        user_id: str = "user-1",
        role: str = "admin",
        email: str = "admin@a.example",
    ) -> Principal:
        token = make_token(tenant_id, user_id, role, email)
        return Principal(
            user_id=user_id, tenant_id=tenant_id, role=role, email=email, token=token  # type: ignore[arg-type]
        )

    return _make


@pytest.fixture
def use_local_verifier(rsa_key: RSAPrivateKey) -> Iterator[JWTVerifier]:
    """Make every service verify tokens with the test public key instead of fetching a JWKS."""
    verifier = JWTVerifier(issuer=TEST_ISSUER, audience=TEST_AUDIENCE, public_key=rsa_key.public_key())
    set_verifier(verifier)
    yield verifier
    set_verifier(None)


# ---------- embeddings and Qdrant (Step 1.3) ----------

FAKE_DIM = 64
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _term_id(term: str) -> int:
    return zlib.crc32(term.encode("utf-8"))


@dataclass
class _FakeSparseEmbedding:
    indices: np.ndarray
    values: np.ndarray


class FakeDenseModel:
    """Deterministic bag-of-words hashing into FAKE_DIM dims; texts sharing words are similar."""

    embedding_size = FAKE_DIM

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(FAKE_DIM, dtype=np.float32)
        for t in _tokens(text):
            h = _term_id(t)
            v[h % FAKE_DIM] += 1.0 if (h >> 8) & 1 else -1.0
        n = float(np.linalg.norm(v))
        if n == 0.0:
            v[0] = 1.0
            n = 1.0
        return v / n

    def passage_embed(self, texts: Iterable[str], **_: Any) -> Iterator[np.ndarray]:
        for t in texts:
            yield self._vec(t)

    def query_embed(self, query: str | Iterable[str], **_: Any) -> Iterator[np.ndarray]:
        for t in [query] if isinstance(query, str) else query:
            yield self._vec(t)


class FakeSparseModel:
    """BM25-like sparse vectors: term frequencies for passages, 1.0 per unique term for queries."""

    @staticmethod
    def _embed(text: str, *, query: bool) -> _FakeSparseEmbedding:
        counts: dict[int, float] = {}
        for t in _tokens(text):
            counts[_term_id(t)] = 1.0 if query else counts.get(_term_id(t), 0.0) + 1.0
        idx = sorted(counts)
        values = np.array([counts[i] for i in idx], dtype=np.float32)
        return _FakeSparseEmbedding(np.array(idx, dtype=np.int64), values)

    def passage_embed(self, texts: Iterable[str], **_: Any) -> Iterator[_FakeSparseEmbedding]:
        for t in texts:
            yield self._embed(t, query=False)

    def query_embed(self, query: str | Iterable[str], **_: Any) -> Iterator[_FakeSparseEmbedding]:
        for t in [query] if isinstance(query, str) else query:
            yield self._embed(t, query=True)


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeDenseModel, FakeSparseModel]:
    """Patch the model loaders in common.embeddings with offline fakes (dense DIM=64)."""
    dense, sparse = FakeDenseModel(), FakeSparseModel()
    monkeypatch.setattr(embeddings, "_dense", lambda: dense)
    monkeypatch.setattr(embeddings, "_sparse", lambda: sparse)
    return dense, sparse


@pytest.fixture
def qdrant_mem(monkeypatch: pytest.MonkeyPatch) -> Iterator[QdrantClient]:
    """In-memory Qdrant patched into common.vectorstore.client, with both collections created."""
    mem = QdrantClient(":memory:")
    monkeypatch.setattr(vectorstore, "client", lambda: mem)
    vectorstore.ensure_chunks_collection(FAKE_DIM)
    vectorstore.ensure_cache_collection(FAKE_DIM)
    yield mem
    mem.close()
