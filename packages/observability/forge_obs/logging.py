"""Structured, redacted, trace-correlated JSON logging (F38 §4).

``configure_logging`` installs one JSON handler on the service's root logger;
every record carries ``service``/``level``/``ts``/``msg``, the current
``trace_id``/``span_id`` (from :mod:`forge_obs.tracing`), and any contextvars
bound via :func:`bind_context` (e.g. ``workspace_id``). The F37 secret redactor
runs LAST over the fully-rendered payload so secrets never reach stdout (and
therefore never reach Loki, whose shipper reads stdout).
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from forge_obs.redaction import redact_value
from forge_obs.settings import ObsSettings
from forge_obs.tracing import current_span_id, current_trace_id

__all__ = ["JsonLogFormatter", "bind_context", "clear_context", "configure_logging", "get_logger"]

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("forge_obs_log_context", default=None)

#: Marker attribute so reconfiguration never stacks duplicate handlers.
_HANDLER_FLAG = "_forge_obs_json_handler"

#: LogRecord attributes that are not user-supplied extras.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


def bind_context(**kv: Any) -> None:
    """Bind key/values (e.g. ``workspace_id=...``) onto subsequent log records."""
    current = _CONTEXT.get() or {}
    _CONTEXT.set({**current, **{k: v for k, v in kv.items() if v is not None}})


def clear_context() -> None:
    """Drop all bound context (end of request/task)."""
    _CONTEXT.set(None)


class JsonLogFormatter(logging.Formatter):
    """Render records as one JSON object per line, secret-redacted last."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = current_trace_id()
        span_id = current_span_id()
        if trace_id:
            payload["trace_id"] = trace_id
        if span_id:
            payload["span_id"] = span_id
        payload.update(_CONTEXT.get() or {})
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        # Redaction runs LAST so secrets never reach any sink (spec AC12).
        return json.dumps(redact_value(payload), default=str, separators=(",", ":"))


def configure_logging(*, service_name: str, settings: ObsSettings | None = None) -> None:
    """Install the JSON handler on the root logger (idempotent).

    JSON to stdout works in every mode — in the lean stack Docker's capped
    json-file logs capture it (spec Journey E); under the observability profile
    the collector ships it to Loki. The root level is lowered to INFO only when
    observability is explicitly enabled; the lean default leaves the process's
    logging level untouched (zero behavioral change when off).
    """
    root = logging.getLogger()
    installed = None
    for handler in root.handlers:
        if getattr(handler, _HANDLER_FLAG, False):
            handler.setFormatter(JsonLogFormatter(service_name))
            installed = handler
            break
    if installed is None:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonLogFormatter(service_name))
        setattr(handler, _HANDLER_FLAG, True)
        root.addHandler(handler)
    if (
        settings is not None
        and settings.enabled
        and (root.level == logging.NOTSET or root.level > logging.INFO)
    ):
        root.setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Namespaced logger (records flow through the configured JSON handler)."""
    return logging.getLogger(name)
