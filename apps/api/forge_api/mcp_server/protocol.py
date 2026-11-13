"""JSON-RPC 2.0 framing for forge's MCP server (spec I §3). Pure: no I/O.

One message per HTTP POST. Batches are refused. A message without ``id`` is a
notification; a message carrying ``result``/``error`` instead of ``method`` is a
client response. Both are acknowledged with HTTP 202 and no body by the router.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

#: Preferred first. ``initialize`` echoes the client's version when listed here.
PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Server-defined: the principal may not use this tool.
FORBIDDEN = -32003

type RequestId = str | int | None


class JsonRpcError(Exception):
    """A protocol-level failure, rendered as a JSON-RPC error object."""

    def __init__(self, code: int, message: str, *, request_id: RequestId = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


@dataclass(frozen=True)
class Message:
    """One parsed inbound message."""

    method: str | None
    request_id: RequestId = None
    params: dict[str, Any] = field(default_factory=dict)
    is_notification: bool = False
    is_response: bool = False

    @property
    def needs_reply(self) -> bool:
        return not (self.is_notification or self.is_response)


def parse_message(body: bytes) -> Message:
    """Parse one JSON-RPC message; raise :class:`JsonRpcError` when malformed."""
    try:
        raw = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise JsonRpcError(PARSE_ERROR, "parse error: body is not valid JSON") from exc
    if isinstance(raw, list):
        raise JsonRpcError(INVALID_REQUEST, "batch requests are not supported")
    if not isinstance(raw, dict) or raw.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
    request_id = raw.get("id")
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, str | int)
    ):
        raise JsonRpcError(INVALID_REQUEST, "id must be a string or an integer")
    method = raw.get("method")
    if method is None:
        if "result" in raw or "error" in raw:
            return Message(method=None, request_id=request_id, is_response=True)
        raise JsonRpcError(INVALID_REQUEST, "missing method", request_id=request_id)
    if not isinstance(method, str):
        raise JsonRpcError(INVALID_REQUEST, "method must be a string", request_id=request_id)
    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise JsonRpcError(INVALID_PARAMS, "params must be an object", request_id=request_id)
    return Message(
        method=method,
        request_id=request_id,
        params=params,
        is_notification="id" not in raw,
    )


def negotiate_version(requested: object) -> str:
    """The client's version when supported, otherwise the preferred one."""
    if isinstance(requested, str) and requested in PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSIONS[0]


def success(request_id: RequestId, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def failure(request_id: RequestId, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


__all__ = [
    "FORBIDDEN",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSIONS",
    "JsonRpcError",
    "Message",
    "failure",
    "negotiate_version",
    "parse_message",
    "success",
]
