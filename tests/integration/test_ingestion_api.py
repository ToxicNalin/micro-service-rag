"""Ingestion service API. Step 3.1: startup, health and migrations; Step 3.4 adds the documents API."""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from common import vectorstore
from ingestion_service import config as ingestion_config
from ingestion_service import db as ingestion_db
from ingestion_service.main import create_app
from ingestion_service.models import Base, Document, DocumentStatus, IngestionJob, JobStatus

INGESTION_DIR = Path(__file__).resolve().parents[2] / "services" / "ingestion"


def _sync_url(async_url: str) -> str:
    return async_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


def _use_database(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    ingestion_config.get_settings.cache_clear()
    ingestion_db.reset_engine()


@pytest.fixture
def ingestion_env(monkeypatch: pytest.MonkeyPatch, sqlite_url: str, fake_embeddings: Any) -> Iterator[str]:
    """Fresh SQLite DB with tables, fake models, and an EMPTY in-memory Qdrant (startup must create it)."""
    _use_database(monkeypatch, sqlite_url)
    mem = QdrantClient(":memory:")
    monkeypatch.setattr(vectorstore, "client", lambda: mem)
    engine = sa.create_engine(_sync_url(sqlite_url))
    Base.metadata.create_all(engine)
    engine.dispose()
    yield sqlite_url
    mem.close()
    ingestion_db.reset_engine()
    ingestion_config.get_settings.cache_clear()


@pytest.fixture
def ingestion_client(ingestion_env: str) -> Iterator[TestClient]:
    with TestClient(create_app()) as client:
        yield client


# ---------- startup and health ----------


def test_startup_creates_chunks_collection(ingestion_client: TestClient) -> None:
    c = vectorstore.client()
    assert c.collection_exists("documents")
    params = c.get_collection("documents").config.params
    assert params.vectors["dense"].size == 64  # type: ignore[index]  (fake model dim)
    assert "sparse" in params.sparse_vectors  # type: ignore[operator]
    assert not c.collection_exists("semantic_cache")  # retrieval owns the cache collection


def test_health_ok(ingestion_client: TestClient) -> None:
    resp = ingestion_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok", "qdrant": "ok"}
    assert len(resp.headers["X-Request-ID"]) == 32


def test_health_reports_qdrant_down(ingestion_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def get_collections(self) -> None:
            raise ConnectionError("qdrant is down")

    monkeypatch.setattr(vectorstore, "client", lambda: Down())
    resp = ingestion_client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["qdrant"] == "down" and resp.json()["db"] == "ok"


def test_health_reports_db_down(ingestion_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ingestion_service.main.ping_db", _false)
    resp = ingestion_client.get("/health")
    assert resp.status_code == 503 and resp.json()["db"] == "down"


async def _false() -> bool:
    return False


def test_startup_fails_if_qdrant_never_comes_up(ingestion_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def never_ready(retries: int = 30, delay: float = 2.0) -> None:
        raise RuntimeError("Qdrant unreachable after 30 attempts")

    monkeypatch.setattr(vectorstore, "wait_for_qdrant", never_ready)
    with pytest.raises(RuntimeError, match="Qdrant unreachable"), TestClient(create_app()):
        pass


def test_unhandled_error_is_generic(ingestion_env: str) -> None:
    app: FastAPI = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom", headers={"X-Request-ID": "req-ing-1"})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "internal error", "request_id": "req-ing-1"}
    assert "must not leak" not in resp.text


# ---------- schema ----------


def test_models_defaults_and_cascade(ingestion_env: str) -> None:
    engine = sa.create_engine(_sync_url(ingestion_env))
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        with sa.orm.Session(bind=conn) as s:
            doc = Document(
                tenant_id=uuid.uuid4(),
                content_hash="a" * 64,
                filename="m.md",
                file_type="md",
                size_bytes=10,
                uploaded_by=uuid.uuid4(),
            )
            s.add(doc)
            s.flush()
            s.add(IngestionJob(tenant_id=doc.tenant_id, document_id=doc.id))
            s.commit()
            assert doc.status == DocumentStatus.processing and doc.chunk_count == 0
            job = s.scalars(sa.select(IngestionJob)).one()
            assert job.status == JobStatus.queued
            s.delete(doc)
            s.commit()
            assert s.scalars(sa.select(IngestionJob)).all() == []  # jobs cascade with the document
    engine.dispose()


def test_same_file_twice_in_one_tenant_is_rejected(ingestion_env: str) -> None:
    engine = sa.create_engine(_sync_url(ingestion_env))
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    def doc(tenant: uuid.UUID) -> Document:
        return Document(
            tenant_id=tenant,
            content_hash="b" * 64,
            filename="x.txt",
            file_type="txt",
            size_bytes=1,
            uploaded_by=uuid.uuid4(),
        )

    with sa.orm.Session(engine) as s:
        s.add_all([doc(tenant_a), doc(tenant_b)])  # the same bytes in two tenants is fine
        s.commit()
        s.add(doc(tenant_a))
        with pytest.raises(sa.exc.IntegrityError):
            s.commit()
    engine.dispose()


def test_migration_matches_models(monkeypatch: pytest.MonkeyPatch, sqlite_url: str) -> None:
    """`alembic upgrade head` must produce exactly the schema the ORM models describe."""
    _use_database(monkeypatch, sqlite_url)
    cfg = Config(str(INGESTION_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(INGESTION_DIR / "migrations"))
    cfg.attributes["database_url"] = sqlite_url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")

    engine = sa.create_engine(_sync_url(sqlite_url))
    with engine.connect() as conn:
        insp = sa.inspect(conn)
        assert {"documents", "ingestion_jobs", "alembic_version"} <= set(insp.get_table_names())
        indexes = {i["name"] for t in ("documents", "ingestion_jobs") for i in insp.get_indexes(t)}
        assert {
            "ix_documents_tenant_id",
            "ix_ingestion_jobs_tenant_id",
            "ix_ingestion_jobs_document_id",
        } <= indexes
        uniques = {u["name"] for u in insp.get_unique_constraints("documents")}
        assert "uq_documents_tenant_hash" in uniques
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migration differ: {diff}"

    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        assert set(sa.inspect(conn).get_table_names()) == {"alembic_version"}
    engine.dispose()
    ingestion_config.get_settings.cache_clear()
