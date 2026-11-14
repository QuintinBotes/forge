"""Forge's own MCP server endpoint (spec I): ``POST /mcp``.

Streamable-HTTP MCP, stateless, JSON responses only. Distinct from the
``mcp`` router, which manages forge's *outbound* connections to other
people's servers (ADR-0006: separate trust directions).

Order of checks: authentication (401, via ``get_current_principal``) → Origin
(403) → body size (413) → JSON-RPC parsing → method dispatch. Every
``tools/call`` writes exactly one audit event carrying a SHA-256 of the
arguments, never the arguments themselves.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from forge_api import __version__
from forge_api.db import get_db
from forge_api.deps import Principal, get_current_principal
from forge_api.mcp_server.protocol import (
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    JsonRpcError,
    Message,
    failure,
    negotiate_version,
    parse_message,
    success,
)
from forge_api.mcp_server.registry import ToolContext, ToolRegistry
from forge_api.observability.audit import AuditCategory, AuditLog, compute_payload_hash
from forge_api.settings import Settings, get_settings

#: Largest accepted request body (spec I §4).
MAX_BODY_BYTES = 256 * 1024

INSTRUCTIONS = (
    "Forge is a software factory. Its world model answers two questions with two "
    "timestamps: `at` is when something was true in the world, `as_of` is what forge "
    "had recorded by then; both default to now. Facts carry evidence. Write tools only "
    "create proposals that a person must accept."
)

router = APIRouter(tags=["mcp-server"])
logger = logging.getLogger(__name__)


@functools.cache
def get_mcp_tool_registry() -> ToolRegistry:
    """The tools this server exposes (spec I §5)."""
    from forge_api.mcp_server.world_tools import WORLD_TOOLS

    return ToolRegistry(WORLD_TOOLS)


@functools.cache
def get_mcp_server_audit_log() -> AuditLog:
    from forge_api.observability.audit_db import default_audit_log

    return default_audit_log()


PrincipalDep = Annotated[Principal, Depends(get_current_principal)]
SessionDep = Annotated[Session, Depends(get_db)]
RegistryDep = Annotated[ToolRegistry, Depends(get_mcp_tool_registry)]
AuditDep = Annotated[AuditLog, Depends(get_mcp_server_audit_log)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _dispatch(
    message: Message,
    *,
    principal: Principal,
    session: Session,
    registry: ToolRegistry,
    audit: AuditLog,
) -> dict[str, Any]:
    method = message.method
    if method == "initialize":
        return {
            "protocolVersion": negotiate_version(message.params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "forge", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": registry.list_payload(principal)}
    if method == "tools/call":
        name = message.params.get("name")
        arguments = message.params.get("arguments")
        started = time.monotonic()
        outcome = "error"
        try:
            result = registry.call(ToolContext(principal, session), name, arguments)
            outcome = "error" if result["isError"] else "ok"
            return result
        except JsonRpcError:
            outcome = "refused"
            raise
        finally:
            audit.record(
                category=AuditCategory.MCP_CALL,
                action="mcp.tool_called",
                actor=str(principal.user_id),
                workspace_id=principal.workspace_id,
                target=name if isinstance(name, str) else None,
                status=outcome,
                payload_hash=compute_payload_hash(arguments),
                latency_ms=int((time.monotonic() - started) * 1000),
                metadata={"server": "forge"},
            )
    raise JsonRpcError(METHOD_NOT_FOUND, f"method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    registry: RegistryDep,
    audit: AuditDep,
    settings: SettingsDep,
) -> Response:
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.mcp_allowed_origins:
        return JSONResponse({"detail": "origin not allowed"}, status.HTTP_403_FORBIDDEN)
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status.HTTP_413_CONTENT_TOO_LARGE)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status.HTTP_413_CONTENT_TOO_LARGE)

    try:
        message = parse_message(body)
    except JsonRpcError as exc:
        return JSONResponse(failure(exc.request_id, exc.code, exc.message))
    if not message.needs_reply:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    try:
        result = await run_in_threadpool(
            _dispatch,
            message,
            principal=principal,
            session=session,
            registry=registry,
            audit=audit,
        )
    except JsonRpcError as exc:
        return JSONResponse(failure(message.request_id, exc.code, exc.message))
    except Exception:  # never leak internals over the wire
        logger.exception("mcp server: %s failed", message.method)
        return JSONResponse(failure(message.request_id, INTERNAL_ERROR, "internal error"))
    return JSONResponse(success(message.request_id, result))


@router.get("/mcp")
def mcp_get(_: PrincipalDep) -> Response:
    """No server-initiated stream in this phase (spec I §3)."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


__all__ = ["MAX_BODY_BYTES", "get_mcp_server_audit_log", "get_mcp_tool_registry", "router"]
