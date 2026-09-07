"""Structured logging with a correlation ID that survives every hop.

The correlation ID is a context variable, so it follows an asyncio task through
every awaited call without being passed as an argument. A service sets it once
per request; every log line and every model call carries it.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nw_correlation_id", default=None
)


def correlation_id() -> str | None:
    return _correlation_id.get()


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def bind_correlation_id(value: str | None = None) -> Iterator[str]:
    """Set the correlation ID for the duration of a block."""
    cid = value or new_correlation_id()
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id(),
        }
        extra = getattr(record, "nw", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = correlation_id() or "-"
        extra = getattr(record, "nw", None)
        tail = f" {json.dumps(extra, default=str)}" if isinstance(extra, dict) else ""
        return f"{record.levelname:<7} {cid} {record.name}: {record.getMessage()}{tail}"


def configure_logging(fmt: str = "json", level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_fields(**fields: object) -> dict[str, dict[str, object]]:
    """Use as `log.info("msg", extra=log_fields(tokens=12))`."""
    return {"nw": fields}
