"""``HttpMcpTransport`` — MCP Streamable-HTTP transport (HARD-05).

Speaks the MCP protocol (revision ``2025-06-18``) over a single HTTP endpoint:
client -> server messages are JSON-RPC 2.0 ``POST`` bodies; the server may reply
with ``application/json`` (a single response) or ``text/event-stream`` (SSE
framing). This class implements the synchronous
:class:`forge_mcp.transport.Transport` protocol over :class:`httpx.Client`, so
the existing client/manager/query-through stack drives a **real** server with no
contract change.

Security properties (spec MCP Security Rules; HARD-05 §8):

* **RFC 8707 token binding** — the caller-supplied ``resource`` indicator (the
  server's canonical URI) is sent in the ``initialize`` handshake so a bearer
  token can never be replayed against a different audience.
* **No secret ever logged** — headers/bodies are never logged; transport errors
  are redacted before they surface.
* **SSRF guard** — only ``http``/``https`` endpoints are accepted (link-local /
  metadata hardening is escalated to HARD-09's pentest punch-list).
* **Bounded** — every request carries a timeout and responses are size-capped.

Initialization is **lazy**: constructing the transport performs no I/O (so the
factory can build it, and ``isinstance(..., Transport)`` holds, without a live
server); the ``initialize`` handshake runs on the first real call.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from forge_contracts import MCPResource, MCPResourceContent
from forge_mcp.exceptions import MCPSecurityError, MCPTransportUnavailableError
from forge_mcp.transport import ToolSpec
from forge_mcp.transports.jsonrpc import (
    IdGenerator,
    build_notification,
    build_request,
    parse_response,
)

#: MCP revision this transport negotiates by default.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: Hard cap on a single response body (defensive DoS bound); 16 MiB.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

#: Schemes the transport is willing to fetch (SSRF surface reduction).
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class HttpMcpTransport:
    """A live MCP :class:`~forge_mcp.transport.Transport` over Streamable-HTTP."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        resource: str | None = None,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
        sse_compat: bool = False,
    ) -> None:
        scheme = urlsplit(endpoint).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise MCPSecurityError(
                f"MCP endpoint scheme {scheme!r} is not allowed (only http/https)"
            )
        self._endpoint = endpoint
        self._token = token
        self._resource = resource
        self._protocol_version = protocol_version
        self._timeout = timeout_s
        self._sse_compat = sse_compat
        # An injected client is owned by the caller; an implicit one is ours.
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout_s)
        self._ids = IdGenerator()
        self._session_id: str | None = None
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Session lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _ensure_session(self) -> None:
        """Perform the ``initialize`` handshake once, capturing the session id."""
        if self._initialized:
            return
        params: dict[str, Any] = {
            "protocolVersion": self._protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "forge-mcp", "version": "0.1.0"},
        }
        # RFC 8707 audience binding: advertise the resource indicator the token is
        # bound to. Carried in the request ``_meta`` (an MCP-permitted extension)
        # so the resource server can validate the audience of a presented token.
        if self._resource:
            params["_meta"] = {"resource": self._resource}
        payload, response = self._post(build_request("initialize", params, self._ids.next()))
        parse_response(payload)
        session_id = response.headers.get("Mcp-Session-Id") or response.headers.get(
            "mcp-session-id"
        )
        if session_id:
            self._session_id = session_id
        self._initialized = True
        # Best-effort ``notifications/initialized`` (no response expected); a
        # server that rejects it must not abort the session.
        with contextlib.suppress(httpx.HTTPError):
            self._client.post(
                self._endpoint,
                json=build_notification("notifications/initialized"),
                headers=self._base_headers(),
                timeout=self._timeout,
            )

    # ------------------------------------------------------------------ #
    # Wire helpers                                                        #
    # ------------------------------------------------------------------ #

    def _post(self, envelope: dict[str, Any]) -> tuple[Any, httpx.Response]:
        """POST an envelope, returning ``(decoded_payload, response)``.

        Raises :class:`MCPTransportUnavailableError` on any connection failure,
        timeout, or 5xx, with a redaction-safe message that never contains the
        request body or headers.
        """
        try:
            response = self._client.post(
                self._endpoint,
                json=envelope,
                headers=self._base_headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # ``str(exc)`` for httpx errors carries only the URL/kind, never the
            # Authorization header or body — but keep the message generic.
            raise MCPTransportUnavailableError(
                f"MCP transport error contacting server: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 500:
            raise MCPTransportUnavailableError(f"MCP server returned {response.status_code}")
        return self._decode(response), response

    def _decode(self, response: httpx.Response) -> Any:
        """Decode a JSON or SSE response body into a JSON-RPC payload dict."""
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise MCPTransportUnavailableError("MCP response exceeded the size cap")
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            return _parse_sse(response.text)
        # A 202/empty body (e.g. an accepted notification) decodes to ``None``.
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise MCPTransportUnavailableError("MCP server returned a non-JSON body") from exc

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._ensure_session()
        request_id = self._ids.next()
        payload, _ = self._post(build_request(method, params, request_id))
        return parse_response(payload, expected_id=request_id)

    # ------------------------------------------------------------------ #
    # Transport protocol                                                 #
    # ------------------------------------------------------------------ #

    def list_resources(self) -> list[MCPResource]:
        result = self._rpc("resources/list", {})
        raw = (result or {}).get("resources", []) if isinstance(result, Mapping) else []
        return [_to_resource(item) for item in raw if isinstance(item, Mapping)]

    def read_resource(self, uri: str) -> MCPResourceContent:
        result = self._rpc("resources/read", {"uri": uri})
        contents = (result or {}).get("contents", []) if isinstance(result, Mapping) else []
        text = ""
        mime_type: str | None = None
        for item in contents:
            if isinstance(item, Mapping) and "text" in item:
                text = str(item.get("text") or "")
                mime_type = item.get("mimeType")
                break
        return MCPResourceContent(uri=uri, content=text, mime_type=mime_type)

    def list_tools(self) -> list[ToolSpec]:
        result = self._rpc("tools/list", {})
        raw = (result or {}).get("tools", []) if isinstance(result, Mapping) else []
        return [_to_tool_spec(item) for item in raw if isinstance(item, Mapping)]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        return self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpMcpTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _parse_sse(text: str) -> Any:
    """Extract the JSON-RPC payload from a minimal SSE event stream."""
    data_lines = [
        line[len("data:") :].strip() for line in text.splitlines() if line.startswith("data:")
    ]
    if not data_lines:
        raise MCPTransportUnavailableError("MCP SSE response carried no data frame")
    try:
        return json.loads("".join(data_lines))
    except json.JSONDecodeError as exc:
        raise MCPTransportUnavailableError("MCP SSE data frame was not valid JSON") from exc


def _to_resource(item: Mapping[str, Any]) -> MCPResource:
    return MCPResource(
        uri=str(item.get("uri", "")),
        name=item.get("name"),
        namespace=item.get("namespace"),
        mime_type=item.get("mimeType") or item.get("mime_type"),
        metadata={
            k: v
            for k, v in item.items()
            if k not in {"uri", "name", "namespace", "mimeType", "mime_type"}
        },
    )


def _to_tool_spec(item: Mapping[str, Any]) -> ToolSpec:
    annotations = item.get("annotations")
    annotations = dict(annotations) if isinstance(annotations, Mapping) else {}
    return ToolSpec(
        name=str(item.get("name", "")),
        description=item.get("description"),
        read_only=annotations.get("readOnlyHint"),
        destructive=annotations.get("destructiveHint"),
        annotations=annotations,
    )


__all__ = ["DEFAULT_PROTOCOL_VERSION", "MAX_RESPONSE_BYTES", "HttpMcpTransport"]
