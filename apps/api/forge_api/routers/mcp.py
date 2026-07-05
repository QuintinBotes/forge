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

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from forge_api.auth.rbac import Permission
from forge_api.db import get_session_factory
from forge_api.deps import Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.schemas.mcp import McpIndexStatus, UpdateConnectionRequest
from forge_api.services import mcp_index_service
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
from forge_contracts.enums import MCPIndexStrategy
from forge_mcp import MCPConnectionManager, TeeAuditLog, live_transport_factory
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
# F20: flipping index_strategy, reindex, and switch-away are admin-only mutations.
AdminDep = Annotated[Principal, Depends(require_permission(Permission.ADMIN))]


def get_mcp_session_factory() -> sessionmaker[Session]:
    """Session factory for the F20 index control plane (overridable in tests)."""
    return get_session_factory()


SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_mcp_session_factory)]


# --------------------------------------------------------------------------- #
# Manager dependency (overridable for tests / Phase-2 transport swap)          #
# --------------------------------------------------------------------------- #


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _mcp_token_resolver(conn: MCPConnection) -> str | None:
    """Resolve a connection's bearer token for the live transport.

    Production resolves ``conn.auth.token_ref`` (``secret://mcp/<slug>``) from the
    per-workspace vault (``APIKeyKind.mcp_token``); the integration lane falls
    back to ``MCP_TOKEN``. The value is read on demand and never held in a module
    global or logged. ``None`` for an unauthenticated connection.
    """
    from forge_contracts import MCPAuthType

    if conn.auth.type is MCPAuthType.NONE:
        return None
    return os.environ.get("MCP_TOKEN")


def _mcp_audit_sink() -> object | None:
    """Build the durable MCP audit bridge when ``FORGE_MCP_AUDIT_BACKEND=db``.

    The bridge forwards every ``MCPAuditEntry`` to the platform
    :class:`~forge_api.observability.AuditLog` (redacted, hash-chained). When the
    backend is ``memory`` (default) the manager keeps only its in-memory trail.
    """
    if os.environ.get("FORGE_MCP_AUDIT_BACKEND", "memory").strip().lower() != "db":
        return None
    from forge_api.observability import MCPAuditSink
    from forge_api.observability.audit_db import default_audit_log

    # ``default_audit_log`` selects the store via ``FORGE_AUDIT_BACKEND`` (default
    # ``memory``); set it to ``db`` for the entries to land in durable Postgres.
    return MCPAuditSink(default_audit_log())


@lru_cache(maxsize=1)
def _mcp_manager_singleton() -> MCPConnectionManager:
    # ALPHA default: NullTransport — registration + metadata work, but any
    # operation needing a live server raises 503 (no real external calls). The
    # live transport is opt-in behind MCP_LIVE_TRANSPORT so it is never implicit.
    if not _env_true("MCP_LIVE_TRANSPORT"):
        return MCPConnectionManager()

    factory = live_transport_factory(
        token_resolver=_mcp_token_resolver,
        timeout_s=float(os.environ.get("MCP_HTTP_TIMEOUT_S", "30")),
        protocol_version=os.environ.get("MCP_PROTOCOL_VERSION", "2025-06-18"),
    )
    sink = _mcp_audit_sink()
    # TeeAuditLog keeps the in-memory trail (so GET …/audit reads back) while
    # forwarding to the durable platform sink when configured.
    audit_log = TeeAuditLog(sink) if sink is not None else None
    return MCPConnectionManager(transport_factory=factory, audit_log=audit_log)


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
        return manager.list_resources(connection_id, namespace, workspace_id=principal.workspace_id)


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


# --------------------------------------------------------------------------- #
# F20: sync-and-index control plane                                            #
# --------------------------------------------------------------------------- #


def _resolve_connection(
    manager: MCPConnectionManager, connection_id: str, workspace_id: uuid.UUID
) -> MCPConnection:
    """Return the tenant-scoped connection (404 if missing/foreign)."""
    with _mcp_errors():
        client = manager.get(connection_id, workspace_id=workspace_id)
    connection = client.connection
    if connection is None:  # pragma: no cover - registered clients are always connected
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="connection not found")
    return connection


@router.patch("/connections/{connection_id}", response_model=MCPConnection)
def update_connection(
    manager: ManagerDep,
    factory: SessionFactoryDep,
    principal: AdminDep,
    connection_id: str,
    request: UpdateConnectionRequest,
) -> MCPConnection:
    """Update a connection. F20: flipping ``index_strategy`` provisions/tears down
    the linked sync-and-index source. ``allow_write`` stays immutable-false."""
    connection = _resolve_connection(manager, connection_id, principal.workspace_id)

    updates: dict[str, object] = {}
    if request.name is not None:
        updates["name"] = request.name
    if request.allowed_namespaces is not None:
        updates["allowed_namespaces"] = request.allowed_namespaces
    if request.index_strategy is not None:
        updates["index_strategy"] = request.index_strategy

    updated = connection.model_copy(update=updates)
    with _mcp_errors():
        manager.register(updated, workspace_id=principal.workspace_id)

    if request.index_strategy is MCPIndexStrategy.SYNC_AND_INDEX:
        source_id = mcp_index_service.ensure_indexed_source(
            factory,
            slug=updated.id,
            workspace_id=principal.workspace_id,
            allowed_namespaces=updated.allowed_namespaces,
            freshness_sla_minutes=updated.freshness_sla_minutes,
        )
        mcp_index_service.enqueue_full_sync(source_id)
    elif request.index_strategy is MCPIndexStrategy.QUERY_THROUGH:
        mcp_index_service.teardown_indexed_source(
            factory, slug=updated.id, workspace_id=principal.workspace_id
        )

    return updated


@router.post(
    "/connections/{connection_id}/index/reindex",
    status_code=status.HTTP_202_ACCEPTED,
)
def reindex_connection(
    manager: ManagerDep,
    factory: SessionFactoryDep,
    principal: AdminDep,
    connection_id: str,
) -> dict[str, str]:
    """Enqueue a full re-sync of an indexed connection (409 if not indexed)."""
    connection = _resolve_connection(manager, connection_id, principal.workspace_id)
    if connection.index_strategy is not MCPIndexStrategy.SYNC_AND_INDEX:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="connection is not in sync_and_index mode",
        )
    source_id = mcp_index_service.ensure_indexed_source(
        factory,
        slug=connection.id,
        workspace_id=principal.workspace_id,
        allowed_namespaces=connection.allowed_namespaces,
        freshness_sla_minutes=connection.freshness_sla_minutes,
    )
    mcp_index_service.enqueue_full_sync(source_id)
    return {"status": "enqueued", "source_id": str(source_id)}


@router.get("/connections/{connection_id}/index", response_model=McpIndexStatus)
def get_connection_index(
    manager: ManagerDep,
    factory: SessionFactoryDep,
    principal: ReaderDep,
    connection_id: str,
) -> McpIndexStatus:
    """Index status for a connection's provisioned sync-and-index source."""
    connection = _resolve_connection(manager, connection_id, principal.workspace_id)
    return mcp_index_service.index_status(
        factory,
        slug=connection.id,
        workspace_id=principal.workspace_id,
        index_strategy=connection.index_strategy,
    )
