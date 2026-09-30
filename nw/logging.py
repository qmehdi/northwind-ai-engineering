"""Structured logging with a correlation ID that survives every hop.

The correlation ID is a context variable, so it follows an asyncio task through
every awaited call without being passed as an argument. A service sets it once
per request; every log line and every model call carries it.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nw_correlation_id", default=None
)
# Fields every line of this process carries: the tenant and the environment of a cohort
# platform (ADR 0009). Bound once at startup by `nw.serving.identity.bind_identity`.
_static: dict[str, object] = {}


def bind_static_fields(**fields: object) -> None:
    """Add fields to every log line from now on; a `None` value removes the field."""
    for key, value in fields.items():
        if value is None:
            _static.pop(key, None)
        else:
            _static[key] = value


def static_fields() -> dict[str, object]:
    return dict(_static)


def correlation_id() -> str | None:
    return _correlation_id.get()


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


# What a caller may send as `x-correlation-id`: short, and only characters that are safe in a
# log line, a span attribute, a header and a file name. Anything else is replaced, so a
# client cannot inject fake log fields, newlines or a path through the header.
CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def valid_correlation_id(value: str | None) -> str | None:
    """`value` when it is a well-formed correlation ID, else None."""
    if value and CORRELATION_ID_RE.fullmatch(value):
        return value
    return None


@contextmanager
def bind_correlation_id(value: str | None = None) -> Iterator[str]:
    """Set the correlation ID for the duration of a block. A missing or malformed value (a
    header is caller input) gets a fresh ID instead."""
    cid = valid_correlation_id(value) or new_correlation_id()
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
            **_static,
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
        merged = {**_static, **(extra if isinstance(extra, dict) else {})}
        tail = f" {json.dumps(merged, default=str)}" if merged else ""
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
