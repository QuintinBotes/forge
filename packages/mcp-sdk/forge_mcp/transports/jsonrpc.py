"""Minimal JSON-RPC 2.0 envelope for the live MCP transports (HARD-05).

MCP speaks JSON-RPC 2.0 over its transports (Streamable-HTTP / stdio). This
module is the wire-format core shared by :mod:`forge_mcp.transports.http` and
:mod:`forge_mcp.transports.stdio`: it builds request/notification envelopes,
manages monotonic ids, and decodes a response — turning a JSON-RPC ``error``
object into a :class:`JsonRpcError`. It performs **no** I/O, so it is trivially
unit-testable and reused by both transports.

Every error surface is redaction-safe: a server error ``message``/``data`` that
echoes a bearer token or ``key=value`` secret is scrubbed via
:func:`forge_mcp.security.redact` / :func:`~forge_mcp.security.redact_text`
before it can reach a raised exception, a log line, or an audit row (spec MCP
rules 4 & 6; HARD-05 AC10/AC11).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping
from typing import Any

from forge_contracts import ForgeError
from forge_mcp.security import redact, redact_text

#: The JSON-RPC protocol version every envelope carries.
JSONRPC_VERSION = "2.0"


class JsonRpcError(ForgeError):
    """A JSON-RPC 2.0 error object returned by the server.

    Carries the numeric ``code`` and optional ``data`` payload alongside the
    (already-redacted) message so callers can branch on protocol error codes
    without ever seeing a secret the server may have echoed back.
    """

    def __init__(self, message: str, *, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class IdGenerator:
    """Thread-unsafe monotonic id source (one per transport instance/session)."""

    def __init__(self) -> None:
        self._counter: Iterator[int] = itertools.count(1)

    def next(self) -> int:
        return next(self._counter)


def build_request(
    method: str, params: Mapping[str, Any] | None, request_id: int | str
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 *request* envelope (expects a response)."""
    envelope: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        envelope["params"] = dict(params)
    return envelope


def build_notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 *notification* envelope (no id, no response)."""
    envelope: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        envelope["params"] = dict(params)
    return envelope


def parse_response(payload: Any, *, expected_id: int | str | None = None) -> Any:
    """Return the ``result`` of a JSON-RPC response or raise :class:`JsonRpcError`.

    The ``error.message``/``error.data`` are redacted defensively before the
    exception is raised so a server that echoes a secret in an error can never
    leak it into a traceback or audit trail.
    """
    if not isinstance(payload, Mapping):
        raise JsonRpcError("malformed JSON-RPC response (expected a JSON object)")
    error = payload.get("error")
    if error is not None:
        if isinstance(error, Mapping):
            code = error.get("code")
            message = str(error.get("message", "unknown JSON-RPC error"))
            data = redact(error.get("data"))
        else:
            code = None
            message = str(error)
            data = None
        raise JsonRpcError(redact_text(message), code=code, data=data)
    if expected_id is not None and "id" in payload and payload["id"] != expected_id:
        raise JsonRpcError(
            f"JSON-RPC response id {payload['id']!r} does not match request id {expected_id!r}"
        )
    return payload.get("result")


__all__ = [
    "JSONRPC_VERSION",
    "IdGenerator",
    "JsonRpcError",
    "build_notification",
    "build_request",
    "parse_response",
]
