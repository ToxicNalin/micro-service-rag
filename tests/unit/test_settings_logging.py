"""Step 1.1: settings loading, JSON logging and the request-ID middleware."""

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from common.logging import (
    JsonFormatter,
    RequestIdMiddleware,
    configure_logging,
    get_request_id,
    request_id_var,
)
from common.settings import (
    BaseServiceSettings,
    clear_settings_caches,
    get_base_settings,
    get_vector_settings,
)

# ---------- settings ----------

def test_settings_read_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "retrieval")
    monkeypatch.setenv("JWT_AUDIENCE", "other-audience")
    clear_settings_caches()
    s = get_base_settings()
    assert s.service_name == "retrieval"
    assert s.jwt_audience == "other-audience"
    assert s.internal_api_token == "test-internal-token"


def test_settings_fail_without_internal_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    clear_settings_caches()
    with pytest.raises(ValidationError):
        get_base_settings()


def test_empty_env_value_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_ISSUER", "")
    assert BaseServiceSettings().jwt_issuer == "docmind-auth"


def test_settings_are_cached_until_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_base_settings()
    assert get_base_settings() is first
    monkeypatch.setenv("SERVICE_NAME", "changed")
    assert get_base_settings().service_name != "changed"
    clear_settings_caches()
    assert get_base_settings().service_name == "changed"


def test_vector_settings_defaults_and_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PER_TENANT_IDF", "false")
    clear_settings_caches()
    v = get_vector_settings()
    assert v.per_tenant_idf is False
    assert v.chunks_collection == "documents"
    assert v.cache_collection == "semantic_cache"
    assert v.dense_model == "BAAI/bge-small-en-v1.5"


# ---------- JSON logging ----------

def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_log_line_is_json_with_request_id() -> None:
    token = request_id_var.set("req-123")
    try:
        line = JsonFormatter("auth").format(_record("user %s", user="u1"))
    finally:
        request_id_var.reset(token)
    data = json.loads(line)
    assert data["request_id"] == "req-123"
    assert data["service"] == "auth"
    assert data["level"] == "INFO"
    assert data["logger"] == "test.logger"
    assert data["msg"] == "user %s"
    assert data["user"] == "u1"
    assert "ts" in data


def test_request_id_defaults_to_dash() -> None:
    assert get_request_id() == "-"
    assert json.loads(JsonFormatter().format(_record()))["request_id"] == "-"


def test_sensitive_extra_fields_are_redacted() -> None:
    line = JsonFormatter().format(
        _record(
            password="hunter2-secret",
            refresh_token="raw-refresh-value",
            headers={"Authorization": "Bearer abc.def.ghi", "Accept": "json"},
        )
    )
    data = json.loads(line)
    assert "hunter2-secret" not in line
    assert "raw-refresh-value" not in line
    assert "abc.def.ghi" not in line
    assert data["headers"]["Accept"] == "json"


def test_exception_is_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in data["exc"]


def test_configure_logging_is_idempotent_and_silences_access() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging("svc", "info")
        configure_logging("svc", "debug")
        ours = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        assert len(ours) == 1
        assert root.level == logging.DEBUG
        assert logging.getLogger("uvicorn.access").disabled is True
        assert isinstance(logging.getLogger("uvicorn.error").handlers[0].formatter, JsonFormatter)
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
        logging.getLogger("uvicorn.access").disabled = False


# ---------- RequestIdMiddleware ----------

def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/rid")
    def rid() -> dict[str, str]:
        return {"request_id": get_request_id()}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    return app


def test_middleware_generates_request_id() -> None:
    with TestClient(_app()) as client:
        resp = client.get("/rid")
    rid = resp.headers["X-Request-ID"]
    assert len(rid) == 32
    assert resp.json()["request_id"] == rid


def test_middleware_echoes_incoming_request_id() -> None:
    with TestClient(_app()) as client:
        resp = client.get("/rid", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"
    assert resp.json()["request_id"] == "abc-123"


def test_middleware_replaces_unsafe_request_id() -> None:
    with TestClient(_app()) as client:
        resp = client.get("/rid", headers={"X-Request-ID": "bad id\twith spaces"})
    assert resp.headers["X-Request-ID"] != "bad id\twith spaces"
    assert len(resp.headers["X-Request-ID"]) == 32


def test_middleware_logs_request(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="common.request"), TestClient(_app()) as client:
        client.get("/rid?token=should-not-be-logged", headers={"X-Request-ID": "log-me"})
    records = [r for r in caplog.records if r.name == "common.request"]
    assert len(records) == 1
    rec = records[0]
    assert (rec.method, rec.path, rec.status) == ("GET", "/rid", 200)
    assert rec.duration_ms >= 0
    assert "should-not-be-logged" not in JsonFormatter().format(rec)


def test_request_id_resets_after_request() -> None:
    with TestClient(_app(), raise_server_exceptions=False) as client:
        client.get("/rid", headers={"X-Request-ID": "temp"})
        assert client.get("/boom").status_code == 500
    assert get_request_id() == "-"
