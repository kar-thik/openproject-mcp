"""Structured logging to stderr (SPEC §12).

Two formats: human-readable lines (default) and JSON lines
(``OPENPROJECT_MCP_LOG_FORMAT=json``). Every log record can carry a
correlation id that ties a tool call to the upstream requests it made.

Credentials never reach a log record: :func:`redact_headers` is the only
sanctioned way to log headers, and request/response bodies are logged only
when ``OPENPROJECT_MCP_LOG_BODIES=1`` **and** the level is DEBUG.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from openproject_mcp.config import Settings

LOGGER_NAME = "openproject_mcp"
REDACTED = "<redacted>"
SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})

_correlation_id: ContextVar[str | None] = ContextVar("openproject_mcp_correlation_id", default=None)

# Record attributes that logging.LogRecord always defines; anything else on a
# record is treated as structured context and emitted as a field.
_STANDARD_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the server's root logger."""
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def current_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Generator[str]:
    """Bind a correlation id for the duration of a tool call."""
    value = correlation_id or uuid.uuid4().hex[:12]
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy headers with credential-bearing values replaced by a placeholder."""
    return {
        key: (REDACTED if key.lower() in SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = _correlation_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with extra record attributes inlined."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Human-readable line with the correlation id and extra fields appended."""

    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in vars(record).items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_") and value is not None
        }
        if not extras:
            return base
        rendered = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{base} [{rendered}]"


def configure_logging(settings: Settings) -> logging.Logger:
    """Attach a single stderr handler to the server logger.

    Idempotent: repeated calls replace the handler rather than stacking. stdio
    transport owns stdout, so logs go to stderr unconditionally (SPEC §3.3).
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())
    handler.addFilter(_CorrelationFilter())
    logger.addHandler(handler)
    logger.setLevel(settings.log_level)
    logger.propagate = False
    return logger
