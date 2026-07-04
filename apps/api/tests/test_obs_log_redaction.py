"""Structural log-redaction tests (HARD-13 AC11).

A ``logging.Filter`` installed on the root + service loggers scrubs secrets from
every emitted record, so an accidental ``logger.info(secret)`` cannot leak.
"""

from __future__ import annotations

import io
import logging

from forge_api.observability.redaction import (
    RedactingLogFilter,
    install_log_redaction,
)


def _capturing_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, stream


def test_filter_redacts_secret_in_positional_args() -> None:
    logger, stream = _capturing_logger("forge.test.redaction.args")
    install_log_redaction(extra_loggers=["forge.test.redaction.args"])
    logger.info("provider key=%s", "sk-deadbeefdeadbeef00")
    out = stream.getvalue()
    assert "[REDACTED]" in out
    assert "sk-deadbeefdeadbeef00" not in out


def test_filter_redacts_secret_in_message_text() -> None:
    logger, stream = _capturing_logger("forge.test.redaction.msg")
    install_log_redaction(extra_loggers=["forge.test.redaction.msg"])
    logger.info("auth header: Bearer abcdef0123456789abcdef")
    out = stream.getvalue()
    assert "[REDACTED]" in out
    assert "abcdef0123456789abcdef" not in out


def test_install_attaches_filter_to_root_uvicorn_celery() -> None:
    install_log_redaction()
    for name in ("", "uvicorn", "celery"):
        logger = logging.getLogger(name)
        assert any(isinstance(f, RedactingLogFilter) for f in logger.filters)


def test_install_is_idempotent() -> None:
    install_log_redaction()
    install_log_redaction()
    root = logging.getLogger("")
    count = sum(isinstance(f, RedactingLogFilter) for f in root.filters)
    assert count == 1
