"""MCP router (Task 1.12 — mcp-sdk + gateway).

Serves the MCP control plane over HTTP: register connections, list/read
namespace-scoped resources, call audited (read-only-by-default) tools, and read
the redacted audit trail. Handlers delegate to a process-wide
:class:`~forge_mcp.MCPConnectionManager` whose default transport is
:class:`~forge_mcp.NullTransport` — so no live MCP traffic ever happens
implicitly (the live transport is injected at the Phase-2 wire-up barrier, and
mocked in tests via ``app.dependency_overrides``).

MCP security rules map to HTTP status codes:

* write tool on a read-only connection / out-of-scope read / policy denial
  / approval required -> 403,
* unknown connection -> 404,
* invalid tool inputs -> 422,
* no live transport configured -> 503.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from forge_api.deps import get_current_principal
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
from forge_mcp import MCPConnectionManager
from forge_mcp.exceptions import (
    MCPConnectionNotFoundError,
    MCPInputError,
    MCPNamespaceError,
    MCPTransportUnavailableError,
)

router = APIRouter(
    prefix="/mcp",
    tags=["mcp"],
    dependencies=[Depends(get_current_principal)],
)


# --------------------------------------------------------------------------- #
# Manager dependency (overridable for tests / Phase-2 transport swap)          #
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _mcp_manager_singleton() -> MCPConnectionManager:
    # Default: NullTransport — registration + metadata work, but any operation
    # needing a live server raises (no real external calls in Phase 1).
    return MCPConnectionManager()


def get_mcp_manager() -> MCPConnectionManager:
    """Return the process-wide MCP connection manager (override in tests via DI)."""
    return _mcp_manager_singleton()


ManagerDep = Annotated[MCPConnectionManager, Depends(get_mcp_manager)]


# --------------------------------------------------------------------------- #
# Error mapping + request bodies                                              #
# --------------------------------------------------------------------------- #


@contextmanager
def _mcp_errors() -> Iterator[None]:
    """Translate MCP SDK exceptions into HTTP error responses."""
    try:
        yield
    except MCPConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        MCPWriteForbiddenError,
        MCPNamespaceError,
        PolicyViolationError,
        ApprovalRequiredError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MCPInputError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except MCPTransportUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


class ToolCallRequest(BaseModel):
    """Body for ``POST /mcp/connections/{connection_id}/tools/call``."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Connections                                                                  #
# --------------------------------------------------------------------------- #


@router.get("/connections", response_model=list[MCPConnection])
def list_connections(manager: ManagerDep) -> list[MCPConnection]:
    return manager.list_connections()


@router.post(
    "/connections",
    response_model=MCPConnection,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(manager: ManagerDep, connection: MCPConnection) -> MCPConnection:
    """Register an MCP connection (read-only by default; RFC 8707 bound on connect)."""
    with _mcp_errors():
        return manager.register(connection)


# --------------------------------------------------------------------------- #
# Resources (namespace-scoped reads)                                          #
# --------------------------------------------------------------------------- #


@router.get(
    "/connections/{connection_id}/resources",
    response_model=list[MCPResource],
)
def list_resources(
    manager: ManagerDep,
    connection_id: str,
    namespace: str | None = Query(default=None),
) -> list[MCPResource]:
    with _mcp_errors():
        return manager.list_resources(connection_id, namespace)


@router.get(
    "/connections/{connection_id}/resources/read",
    response_model=MCPResourceContent,
)
def read_resource(
    manager: ManagerDep,
    connection_id: str,
    uri: str = Query(...),
) -> MCPResourceContent:
    with _mcp_errors():
        return manager.read_resource(connection_id, uri)


# --------------------------------------------------------------------------- #
# Tools (write-gated, policy-checked, audited)                                 #
# --------------------------------------------------------------------------- #


@router.post(
    "/connections/{connection_id}/tools/call",
    response_model=MCPToolResult,
)
def call_tool(
    manager: ManagerDep,
    connection_id: str,
    request: ToolCallRequest,
) -> MCPToolResult:
    with _mcp_errors():
        return manager.call_tool(connection_id, request.name, request.arguments)


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #


@router.get(
    "/connections/{connection_id}/audit",
    response_model=list[MCPAuditEntry],
)
def list_audit(manager: ManagerDep, connection_id: str) -> list[MCPAuditEntry]:
    with _mcp_errors():
        # Validate the connection exists (raises 404 if not), then return its trail.
        manager.get(connection_id)
        return manager.audit_entries(connection_id)
