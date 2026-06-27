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

from forge_api.auth.rbac import Permission
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
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

# RBAC + tenancy (Phase-2 bug fix r3). The MCP control plane was auth-only, so a
# read-only ``viewer`` could register connections (and flip ``allow_write``) and
# call tools. These permission-gated principals authorize per-route *and* carry
# the caller's ``workspace_id`` so every operation is scoped to the tenant that
# owns the connection (cross-tenant enumeration/read/call/audit is impossible):
#
# * READ      — list/read resources + audit (all roles hold READ),
# * WRITE     — register a connection (config mutation; viewer/agent-runner 403),
# * RUN_AGENT — call a tool (an action; member/agent-runner/admin, viewer 403).
ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
RunnerDep = Annotated[Principal, Depends(require_permission(Permission.RUN_AGENT))]


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
def list_connections(manager: ManagerDep, principal: ReaderDep) -> list[MCPConnection]:
    return manager.list_connections(workspace_id=principal.workspace_id)


@router.post(
    "/connections",
    response_model=MCPConnection,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    manager: ManagerDep, principal: WriterDep, connection: MCPConnection
) -> MCPConnection:
    """Register an MCP connection (read-only by default; RFC 8707 bound on connect)."""
    with _mcp_errors():
        return manager.register(connection, workspace_id=principal.workspace_id)


# --------------------------------------------------------------------------- #
# Resources (namespace-scoped reads)                                          #
# --------------------------------------------------------------------------- #


@router.get(
    "/connections/{connection_id}/resources",
    response_model=list[MCPResource],
)
def list_resources(
    manager: ManagerDep,
    principal: ReaderDep,
    connection_id: str,
    namespace: str | None = Query(default=None),
) -> list[MCPResource]:
    with _mcp_errors():
        return manager.list_resources(
            connection_id, namespace, workspace_id=principal.workspace_id
        )


@router.get(
    "/connections/{connection_id}/resources/read",
    response_model=MCPResourceContent,
)
def read_resource(
    manager: ManagerDep,
    principal: ReaderDep,
    connection_id: str,
    uri: str = Query(...),
) -> MCPResourceContent:
    with _mcp_errors():
        return manager.read_resource(connection_id, uri, workspace_id=principal.workspace_id)


# --------------------------------------------------------------------------- #
# Tools (write-gated, policy-checked, audited)                                 #
# --------------------------------------------------------------------------- #


@router.post(
    "/connections/{connection_id}/tools/call",
    response_model=MCPToolResult,
)
def call_tool(
    manager: ManagerDep,
    principal: RunnerDep,
    connection_id: str,
    request: ToolCallRequest,
) -> MCPToolResult:
    with _mcp_errors():
        return manager.call_tool(
            connection_id,
            request.name,
            request.arguments,
            workspace_id=principal.workspace_id,
        )


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #


@router.get(
    "/connections/{connection_id}/audit",
    response_model=list[MCPAuditEntry],
)
def list_audit(
    manager: ManagerDep, principal: ReaderDep, connection_id: str
) -> list[MCPAuditEntry]:
    with _mcp_errors():
        # Validate the connection exists *and* belongs to the caller's workspace
        # (raises 404 otherwise), then return its trail.
        manager.get(connection_id, workspace_id=principal.workspace_id)
        return manager.audit_entries(connection_id, workspace_id=principal.workspace_id)
