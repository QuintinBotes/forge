"""The Forge MCP gateway FastAPI service (plan Task 1.12).

Exposes the MCP control plane over HTTP: register connections, list/read
namespace-scoped resources, call audited (read-only-by-default) tools, and read
the redacted audit trail. Security errors map to precise HTTP status codes:

* write tool on a read-only connection / out-of-scope read / policy denial -> 403
* unknown connection -> 404
* invalid tool inputs -> 422
* no live transport configured -> 503
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from forge_contracts import (
    ApprovalRequiredError,
    MCPAuditEntry,
    MCPConnection,
    MCPResource,
    MCPResourceContent,
    MCPToolResult,
    MCPWriteForbiddenError,
    PolicyViolationError,
)
from forge_mcp.exceptions import (
    MCPConnectionNotFoundError,
    MCPInputError,
    MCPNamespaceError,
    MCPTransportUnavailableError,
)
from forge_mcp_gateway import __version__
from forge_mcp_gateway.manager import MCPConnectionManager
from forge_mcp_gateway.observability import record_tool_call, setup_gateway_telemetry


class ToolCallRequest(BaseModel):
    """Body for a tool-call request."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _error_handlers(app: FastAPI) -> None:
    """Map SDK security exceptions to HTTP responses."""

    def _json(status_code: int, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    @app.exception_handler(MCPConnectionNotFoundError)
    async def _not_found(_: Request, exc: MCPConnectionNotFoundError) -> JSONResponse:
        return _json(status.HTTP_404_NOT_FOUND, exc)

    @app.exception_handler(MCPWriteForbiddenError)
    async def _write_forbidden(_: Request, exc: MCPWriteForbiddenError) -> JSONResponse:
        return _json(status.HTTP_403_FORBIDDEN, exc)

    @app.exception_handler(MCPNamespaceError)
    async def _namespace(_: Request, exc: MCPNamespaceError) -> JSONResponse:
        return _json(status.HTTP_403_FORBIDDEN, exc)

    @app.exception_handler(PolicyViolationError)
    async def _policy(_: Request, exc: PolicyViolationError) -> JSONResponse:
        return _json(status.HTTP_403_FORBIDDEN, exc)

    @app.exception_handler(ApprovalRequiredError)
    async def _approval(_: Request, exc: ApprovalRequiredError) -> JSONResponse:
        return _json(status.HTTP_403_FORBIDDEN, exc)

    @app.exception_handler(MCPInputError)
    async def _input(_: Request, exc: MCPInputError) -> JSONResponse:
        return _json(status.HTTP_422_UNPROCESSABLE_ENTITY, exc)

    @app.exception_handler(MCPTransportUnavailableError)
    async def _transport(_: Request, exc: MCPTransportUnavailableError) -> JSONResponse:
        return _json(status.HTTP_503_SERVICE_UNAVAILABLE, exc)


def create_gateway_app(manager: MCPConnectionManager | None = None) -> FastAPI:
    """Build the MCP gateway app bound to ``manager`` (a default one if omitted)."""
    # F38 + HARD-10: shared telemetry init (env-driven; no-op by default, real
    # OTLP export + inbound W3C trace-context continuation when enabled).
    setup_gateway_telemetry()
    mgr = manager or MCPConnectionManager()
    app = FastAPI(
        title="Forge MCP Gateway",
        version=__version__,
        description="MCP client manager: read-only by default, audited, namespace-scoped.",
    )

    def get_manager() -> MCPConnectionManager:
        return mgr

    ManagerDep = Depends(get_manager)

    _error_handlers(app)

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "forge-mcp-gateway"}

    @app.get("/connections", response_model=list[MCPConnection], tags=["mcp"])
    def list_connections(manager: MCPConnectionManager = ManagerDep) -> list[MCPConnection]:
        return manager.list_connections()

    @app.post(
        "/connections",
        response_model=MCPConnection,
        status_code=status.HTTP_201_CREATED,
        tags=["mcp"],
    )
    def register_connection(
        connection: MCPConnection, manager: MCPConnectionManager = ManagerDep
    ) -> MCPConnection:
        return manager.register(connection)

    @app.get(
        "/connections/{connection_id}/resources",
        response_model=list[MCPResource],
        tags=["mcp"],
    )
    def list_resources(
        connection_id: str,
        namespace: str | None = Query(default=None),
        manager: MCPConnectionManager = ManagerDep,
    ) -> list[MCPResource]:
        return manager.list_resources(connection_id, namespace)

    @app.get(
        "/connections/{connection_id}/resources/read",
        response_model=MCPResourceContent,
        tags=["mcp"],
    )
    def read_resource(
        connection_id: str,
        uri: str = Query(...),
        manager: MCPConnectionManager = ManagerDep,
    ) -> MCPResourceContent:
        return manager.read_resource(connection_id, uri)

    @app.post(
        "/connections/{connection_id}/tools/call",
        response_model=MCPToolResult,
        tags=["mcp"],
    )
    def call_tool(
        connection_id: str,
        request: ToolCallRequest,
        manager: MCPConnectionManager = ManagerDep,
    ) -> MCPToolResult:
        # F38: every tool call emits forge_mcp_calls_total + latency through
        # the facade (no-op when observability is disabled). The label is the
        # connection *name* (bounded by the cardinality guard), never a
        # payload; emission never raises.
        started = time.perf_counter()
        status_label = "ok"
        try:
            return manager.call_tool(connection_id, request.name, request.arguments)
        except Exception:
            status_label = "error"
            raise
        finally:
            try:
                conn = manager.get(connection_id).connection
                connection_name = conn.name if conn is not None else "unknown"
            except Exception:
                connection_name = "unknown"
            record_tool_call(
                connection=connection_name,
                status=status_label,
                latency_seconds=time.perf_counter() - started,
            )

    @app.get(
        "/connections/{connection_id}/audit",
        response_model=list[MCPAuditEntry],
        tags=["mcp"],
    )
    def audit(
        connection_id: str, manager: MCPConnectionManager = ManagerDep
    ) -> list[MCPAuditEntry]:
        # Validate the connection exists (raises 404 if not), then return its trail.
        manager.get(connection_id)
        return manager.audit_entries(connection_id)

    return app


app = create_gateway_app()


__all__ = ["ToolCallRequest", "app", "create_gateway_app"]
