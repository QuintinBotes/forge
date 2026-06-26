"""MCP SDK router stubs (filled by Task 1.12 — mcp-sdk + gateway).

Read-only by default; tool calls are audited. The gateway service performs the
live transport — these routes are the control-plane surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from forge_api._stubs import NotImplementedResponse, eventual, not_implemented
from forge_api.deps import CurrentPrincipal, get_current_principal
from forge_contracts import (
    MCPConnection,
    MCPResource,
    MCPResourceContent,
    MCPToolResult,
)

router = APIRouter(
    prefix="/mcp",
    tags=["mcp"],
    dependencies=[Depends(get_current_principal)],
    responses={501: {"model": NotImplementedResponse}},
)

_R = "mcp"


@router.get(
    "/connections",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MCPConnection, "List registered MCP connections."),
)
def list_connections(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_connections")


@router.post(
    "/connections",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MCPConnection, "Register an MCP connection (read-only default)."),
)
def create_connection(principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "connect")


@router.get(
    "/connections/{connection_id}/resources",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MCPResource, "List resources, scoped by namespace."),
)
def list_resources(connection_id: str, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "list_resources")


@router.get(
    "/connections/{connection_id}/resources/read",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MCPResourceContent, "Read a resource's content by uri."),
)
def read_resource(connection_id: str, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "read_resource")


@router.post(
    "/connections/{connection_id}/tools/call",
    response_model=NotImplementedResponse,
    status_code=501,
    responses=eventual(MCPToolResult, "Call an MCP tool (audited; write-gated)."),
)
def call_tool(connection_id: str, principal: CurrentPrincipal) -> NotImplementedResponse:
    return not_implemented(_R, "call_tool")
