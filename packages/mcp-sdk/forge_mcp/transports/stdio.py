"""``StdioMcpTransport`` — MCP transport over a subprocess' stdio (HARD-05).

Spawns an MCP server process and exchanges newline-delimited JSON-RPC 2.0
messages over its stdin/stdout, implementing the synchronous
:class:`forge_mcp.transport.Transport` protocol. Used for locally-run MCP
servers (the in-repo reference server, or an off-the-shelf stdio server such as
the filesystem/everything servers).

For stdio there is no network audience, so RFC 8707 token binding is N/A — but
read-only-by-default, namespace scoping, redacted audit, and input validation
(all enforced by the client wrapping this transport) still apply.

The subprocess is spawned **lazily** on first use with a constrained ``env`` and
``cwd`` and is torn down on :meth:`close`; construction performs no I/O.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from forge_contracts import MCPResource, MCPResourceContent
from forge_mcp.exceptions import MCPSecurityError, MCPTransportUnavailableError
from forge_mcp.transport import ToolSpec
from forge_mcp.transports.http import _to_resource, _to_tool_spec
from forge_mcp.transports.jsonrpc import (
    IdGenerator,
    build_notification,
    build_request,
    parse_response,
)

DEFAULT_PROTOCOL_VERSION = "2025-06-18"


class StdioMcpTransport:
    """A live MCP :class:`~forge_mcp.transport.Transport` over a stdio subprocess."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
    ) -> None:
        command = list(command)
        if not command:
            raise MCPSecurityError("stdio transport requires a non-empty command")
        self._command = command
        self._cwd = cwd
        # Constrained env: an explicit mapping when given, else the inherited env.
        self._env = dict(env) if env is not None else None
        self._timeout = timeout_s
        self._protocol_version = protocol_version
        self._ids = IdGenerator()
        self._proc: subprocess.Popen[str] | None = None
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Process lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def _spawn(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self._cwd,
                env={**os.environ, **self._env} if self._env is not None else None,
            )
        except (OSError, ValueError) as exc:
            raise MCPTransportUnavailableError(
                f"failed to spawn MCP stdio server: {type(exc).__name__}"
            ) from exc
        return self._proc

    def _ensure_session(self) -> None:
        if self._initialized:
            return
        self._spawn()
        params: dict[str, Any] = {
            "protocolVersion": self._protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "forge-mcp", "version": "0.1.0"},
        }
        payload = self._exchange(build_request("initialize", params, self._ids.next()))
        parse_response(payload)
        self._initialized = True
        self._send(build_notification("notifications/initialized"))

    # ------------------------------------------------------------------ #
    # Wire helpers                                                        #
    # ------------------------------------------------------------------ #

    def _send(self, envelope: dict[str, Any]) -> None:
        proc = self._spawn()
        if proc.stdin is None:  # pragma: no cover - defensive
            raise MCPTransportUnavailableError("MCP stdio server has no stdin")
        try:
            proc.stdin.write(json.dumps(envelope) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise MCPTransportUnavailableError("MCP stdio server closed its input") from exc

    def _exchange(self, envelope: dict[str, Any]) -> Any:
        """Send a request and read exactly one response line (bounded wait)."""
        proc = self._spawn()
        self._send(envelope)
        if proc.stdout is None:  # pragma: no cover - defensive
            raise MCPTransportUnavailableError("MCP stdio server has no stdout")
        ready, _, _ = select.select([proc.stdout], [], [], self._timeout)
        if not ready:
            raise MCPTransportUnavailableError("MCP stdio server timed out")
        line = proc.stdout.readline()
        if not line:
            raise MCPTransportUnavailableError("MCP stdio server closed its output")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPTransportUnavailableError("MCP stdio server returned a non-JSON line") from exc

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._ensure_session()
        request_id = self._ids.next()
        payload = self._exchange(build_request(method, params, request_id))
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
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):  # pragma: no cover - best effort
                    stream.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - best effort
                proc.kill()

    def __enter__(self) -> StdioMcpTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["DEFAULT_PROTOCOL_VERSION", "StdioMcpTransport"]
