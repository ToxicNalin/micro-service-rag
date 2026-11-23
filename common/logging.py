"""JSON logs with a request ID that follows a request across services."""

import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_log = logging.getLogger("common.request")

# Attributes every LogRecord has; anything else on a record came from `extra={...}`.
_STANDARD_ATTRS = set(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))) | {
    "message",
    "asctime",
    "taskName",
}
# Extra fields whose values must never reach the logs (I8).
_SENSITIVE_KEY = re.compile(r"authorization|password|passwd|secret|token|cookie|api_key|apikey", re.I)
_REDACTED = "[redacted]"
# An incoming X-Request-ID is reused only if it is short and safe to put in logs and headers.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_HANDLER_MARK = "_docmind_json_handler"


def get_request_id() -> str:
    """Return the current request ID ("-" outside a request)."""
    return request_id_var.get()


def _redact(value: Any, key: str = "") -> Any:
    if key and _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    """Formats each log record as one JSON line."""

    def __init__(self, service: str = "service") -> None:
        """Create a formatter that stamps every line with `service`."""
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as JSON: ts, level, service, logger, msg, request_id and extra fields."""
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in entry or key.startswith("_"):
                continue
            entry[key] = _redact(value, key)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(service_name: str, level: str) -> None:
    """Install JsonFormatter on the root and uvicorn loggers and silence uvicorn.access."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
        lg.setLevel(level.upper())

    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True


class RequestIdMiddleware:
    """Pure ASGI middleware: sets the request ID, echoes it in X-Request-ID, logs each request."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an ASGI app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI call."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for name, value in scope.get("headers") or []:
            if name == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex

        ctx_token = request_id_var.set(request_id)
        start = time.perf_counter()
        started = False

        def log_request(status: int) -> None:
            _log.info(
                "%s %s %s",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status,
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),  # never the query string: it may carry secrets
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
                log_request(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not started:
                log_request(500)
            raise
        finally:
            request_id_var.reset(ctx_token)
