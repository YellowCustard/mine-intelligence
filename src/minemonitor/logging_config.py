"""Structured JSON logging. Every operational line can carry site_id/asset_id."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}

# Request correlation id, set per request by the API middleware and surfaced on
# every log line emitted while handling that request (brief §25).
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_RID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def sanitize_request_id(value: str | None) -> str | None:
    """Accept a client-supplied request id only if it is safe to log verbatim.

    A bounded, restricted character set stops header-borne log injection — an
    untrusted ``X-Request-ID`` cannot smuggle newlines or JSON control characters
    into the structured log.
    """
    return value if value and _RID_RE.match(value) else None


class RequestIdFilter(logging.Filter):
    """Attach the current request id (when set) to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        if rid is not None:
            record.request_id = rid
        return True


class JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Surface any structured extras (e.g. site_id, asset_id).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
