"""Self-hosted reference MCP server (HARD-05 integration substrate).

A tiny, dependency-free MCP server that speaks the **real** JSON-RPC 2.0 wire
protocol (MCP revision ``2025-06-18``) over two real transports:

* **Streamable-HTTP** — a stdlib :class:`http.server.ThreadingHTTPServer`;
* **stdio** — newline-delimited JSON-RPC over ``stdin``/``stdout``.

It is the integration substrate for HARD-05: the live transports drive it over a
real socket / real subprocess, so "live MCP" is proven end-to-end **without any
external SaaS or credential** (the only "cred" is a local URL). It is seeded with
the same corpus the security tests trust — multiple namespaces
(``engineering`` / ``architecture`` / ``finance``), a read tool + a write tool,
and one resource containing a *planted fake secret* so the redaction path is
exercised on server-authored bytes.

The HTTP server exposes a ``GET /inspect`` endpoint (and an ``_forge/inspect``
JSON-RPC method) that reports the last ``initialize`` handshake it saw — the
captured RFC 8707 ``resource`` indicator and whether an ``Authorization`` header
was present (never the token value) — so a test can assert token-binding was
sent over the wire (HARD-05 AC6).

Run it:

    python -m forge_mcp.reference_server --http --host 127.0.0.1 --port 8901
    python -m forge_mcp.reference_server --stdio
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: A deliberately fake, secret-shaped string planted in a resource so redaction
#: is exercised against bytes the SDK did not author. NOT a real credential.
PLANTED_SECRET = "Authorization: Bearer sk-reference-secret-DO-NOT-USE-000"

SERVER_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "forge-reference-mcp", "version": "0.1.0"}


def _default_resources() -> list[dict[str, Any]]:
    return [
        {
            "uri": "confluence://engineering/page-1",
            "name": "Vault Rotation Runbook",
            "namespace": "engineering",
            "mimeType": "text/plain",
        },
        {
            "uri": "confluence://engineering/page-2",
            "name": "Service Page",
            "namespace": "engineering",
            "mimeType": "text/plain",
        },
        {
            "uri": "confluence://architecture/adr-7",
            "name": "ADR 7",
            "namespace": "architecture",
            "mimeType": "text/plain",
        },
        {
            "uri": "confluence://finance/budget",
            "name": "Budget",
            "namespace": "finance",
            "mimeType": "text/plain",
        },
    ]


def _default_contents() -> dict[str, str]:
    return {
        "confluence://engineering/page-1": f"How to rotate the vault token. {PLANTED_SECRET}",
        "confluence://engineering/page-2": "A general engineering page about services.",
        "confluence://architecture/adr-7": "Architecture decision record number seven.",
        "confluence://finance/budget": "Quarterly budget figures.",
    }


def _default_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "search_pages",
            "description": "Search pages",
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "get_document",
            "description": "Read a document",
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "create_page",
            "description": "Create a page",
            "annotations": {"readOnlyHint": False, "destructiveHint": True},
        },
    ]


@dataclass
class Inspection:
    """The last handshake this server observed (secret-free, for AC6)."""

    resource_indicator: str | None = None
    had_authorization: bool = False
    session_id: str | None = None
    protocol_version: str | None = None


@dataclass
class ReferenceMCPServer:
    """The transport-agnostic JSON-RPC core (pure ``handle`` dispatch)."""

    resources: list[dict[str, Any]] = field(default_factory=_default_resources)
    contents: dict[str, str] = field(default_factory=_default_contents)
    tools: list[dict[str, Any]] = field(default_factory=_default_tools)
    session_id: str = "forge-reference-session"
    inspection: Inspection = field(default_factory=Inspection)

    def handle(
        self, request: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
    ) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC message; return a response, or ``None`` for a
        notification (no ``id``)."""
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            # A notification (e.g. ``notifications/initialized``): no reply.
            return None
        try:
            result = self._dispatch(method, request.get("params") or {}, headers or {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": exc.as_object()}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: Any, params: Mapping[str, Any], headers: Mapping[str, str]) -> Any:
        if method == "initialize":
            meta = params.get("_meta") or {}
            self.inspection = Inspection(
                resource_indicator=meta.get("resource") if isinstance(meta, Mapping) else None,
                had_authorization=bool(_header(headers, "authorization")),
                session_id=self.session_id,
                protocol_version=params.get("protocolVersion"),
            )
            return {
                "protocolVersion": SERVER_PROTOCOL_VERSION,
                "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": SERVER_INFO,
            }
        if method == "resources/list":
            return {"resources": list(self.resources)}
        if method == "resources/read":
            uri = params.get("uri")
            if uri not in self.contents:
                raise _RpcError(-32602, f"unknown resource uri: {uri}")
            return {
                "contents": [{"uri": uri, "mimeType": "text/plain", "text": self.contents[uri]}]
            }
        if method == "tools/list":
            return {"tools": list(self.tools)}
        if method == "tools/call":
            name = params.get("name")
            known = {t["name"] for t in self.tools}
            if name not in known:
                raise _RpcError(-32602, f"unknown tool: {name}")
            return {"content": [{"type": "text", "text": f"invoked {name}"}], "isError": False}
        if method == "_forge/inspect":
            return self.inspect_dict()
        raise _RpcError(-32601, f"method not found: {method}")

    def inspect_dict(self) -> dict[str, Any]:
        i = self.inspection
        return {
            "resource_indicator": i.resource_indicator,
            "had_authorization": i.had_authorization,
            "session_id": i.session_id,
            "protocol_version": i.protocol_version,
        }


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_object(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


# --------------------------------------------------------------------------- #
# HTTP transport                                                              #
# --------------------------------------------------------------------------- #


def _make_handler(core: ReferenceMCPServer) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _write_json(
            self, status: int, body: Any, extra_headers: dict[str, str] | None = None
        ) -> None:
            data = json.dumps(body).encode("utf-8") if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                request = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._write_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    },
                )
                return
            headers = dict(self.headers.items())
            response = core.handle(request, headers=headers)
            if response is None:
                # Notification: acknowledge with 202 and no body.
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            extra = {}
            if request.get("method") == "initialize":
                extra["Mcp-Session-Id"] = core.session_id
            self._write_json(200, response, extra)

        def do_GET(self) -> None:
            if self.path.rstrip("/") == "/inspect":
                self._write_json(200, core.inspect_dict())
                return
            if self.path.rstrip("/") in ("", "/health"):
                self._write_json(200, {"status": "ok", "service": "forge-reference-mcp"})
                return
            self._write_json(404, {"error": "not found"})

        def log_message(self, *args: Any) -> None:  # silence access logging
            return

    return _Handler


@dataclass
class RunningHttpServer:
    """A live reference server on a real loopback socket (for tests / CLI)."""

    core: ReferenceMCPServer
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"http://{host}:{port}/mcp"

    @property
    def inspect_url(self) -> str:
        host, port = self.server.server_address[:2]
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"http://{host}:{port}/inspect"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_http_server(host: str = "127.0.0.1", port: int = 0) -> RunningHttpServer:
    """Start the reference server in a background thread on a real socket."""
    core = ReferenceMCPServer()
    server = ThreadingHTTPServer((host, port), _make_handler(core))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RunningHttpServer(core=core, server=server, thread=thread)


def run_http(host: str = "127.0.0.1", port: int = 8901) -> None:  # pragma: no cover - CLI
    core = ReferenceMCPServer()
    server = ThreadingHTTPServer((host, port), _make_handler(core))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --------------------------------------------------------------------------- #
# stdio transport                                                            #
# --------------------------------------------------------------------------- #


def run_stdio(core: ReferenceMCPServer | None = None) -> None:
    """Serve newline-delimited JSON-RPC over stdin/stdout until EOF."""
    core = core or ReferenceMCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        response = core.handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="Forge reference MCP server")
    parser.add_argument("--http", action="store_true", help="serve over Streamable-HTTP")
    parser.add_argument("--stdio", action="store_true", help="serve over stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args(argv)
    if args.stdio:
        run_stdio()
        return 0
    run_http(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())


__all__ = [
    "PLANTED_SECRET",
    "Inspection",
    "ReferenceMCPServer",
    "RunningHttpServer",
    "run_http",
    "run_stdio",
    "start_http_server",
]
