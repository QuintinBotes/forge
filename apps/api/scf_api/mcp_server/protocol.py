"""JSON-RPC 2.0 framing for SCF's MCP server (spec I §3). Pure: no I/O.

One message per HTTP POST. Batches are refused. A message without ``id`` is a
notification; a message carrying ``result``/``error`` instead of ``method`` is a
client response. Both are acknowledged with HTTP 202 and no body by the router.

Two eras share the endpoint. A request that declares its version per request
(in ``params._meta``, or with a current ``MCP-Protocol-Version`` header) is
served at :data:`CURRENT_VERSION`. Everything else follows the ``initialize``
handshake of :data:`PROTOCOL_VERSIONS`, unchanged.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: The revision marked Current on the MCP versioning page, checked 2026-11-17 (ADR-0016).
CURRENT_VERSION = "2026-07-28"
#: Handshake-era revisions, preferred first. ``initialize`` echoes the client's
#: version when listed here.
PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS: tuple[str, ...] = (CURRENT_VERSION, *PROTOCOL_VERSIONS)

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Server-defined: the principal may not use this tool.
FORBIDDEN = -32003
#: Current revision: a mirrored header is missing or differs from the body.
HEADER_MISMATCH = -32020
#: Current revision: the declared protocol version is not one this server speaks.
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: The body field each named method mirrors into the ``Mcp-Name`` header.
_NAMED_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

type RequestId = str | int | None


class JsonRpcError(Exception):
    """A protocol-level failure, rendered as a JSON-RPC error object."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        request_id: RequestId = None,
        data: Any = None,
        http_status: int = 200,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.data = data
        self.http_status = http_status


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


def _mismatch(message: str) -> JsonRpcError:
    return JsonRpcError(HEADER_MISMATCH, f"header mismatch: {message}", http_status=400)


def _header(headers: Mapping[str, str], name: str, *, encoded: bool = False) -> str | None:
    """A header value without surrounding whitespace; ``encoded`` decodes the Base64 sentinel."""
    value = headers.get(name)
    if value is None:
        return None
    value = value.strip()
    if encoded and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise _mismatch(f"{name} is not valid Base64") from exc
    return value


def modern_version(headers: Mapping[str, str], message: Message) -> str | None:
    """The version a current-era request declares, or ``None`` for a legacy request.

    ``headers`` must look names up case-insensitively, as Starlette's ``Headers``
    does. Raises, with HTTP 400, when the request metadata is incomplete
    (``-32602``), a mirrored header differs from the body (``-32020``) or the
    version is not supported (``-32022``).
    """
    meta = message.params.get("_meta")
    header_version = _header(headers, "mcp-protocol-version")
    declares = isinstance(meta, dict) and META_PROTOCOL_VERSION in meta
    if not declares and header_version != CURRENT_VERSION:
        return None
    if not (
        isinstance(meta, dict)
        and isinstance(meta.get(META_PROTOCOL_VERSION), str)
        and isinstance(meta.get(META_CLIENT_CAPABILITIES), dict)
    ):
        raise JsonRpcError(
            INVALID_PARAMS,
            f"params._meta must carry {META_PROTOCOL_VERSION} and {META_CLIENT_CAPABILITIES}",
            http_status=400,
        )
    version: str = meta[META_PROTOCOL_VERSION]
    if header_version != version:
        raise _mismatch("MCP-Protocol-Version does not match params._meta")
    if _header(headers, "mcp-method") != message.method:
        raise _mismatch("Mcp-Method does not match the body method")
    named = _NAMED_FIELD.get(message.method or "")
    if named and _header(headers, "mcp-name", encoded=True) != message.params.get(named):
        raise _mismatch(f"Mcp-Name does not match params.{named}")
    if version not in SUPPORTED_VERSIONS:
        raise JsonRpcError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "unsupported protocol version",
            data={"supported": list(SUPPORTED_VERSIONS), "requested": version},
            http_status=400,
        )
    return version


def success(request_id: RequestId, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def failure(request_id: RequestId, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


__all__ = [
    "CURRENT_VERSION",
    "FORBIDDEN",
    "HEADER_MISMATCH",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "META_CLIENT_CAPABILITIES",
    "META_PROTOCOL_VERSION",
    "META_SERVER_INFO",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSIONS",
    "SUPPORTED_VERSIONS",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "JsonRpcError",
    "Message",
    "failure",
    "modern_version",
    "negotiate_version",
    "parse_message",
    "success",
]
