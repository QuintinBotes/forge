"""SCF's own MCP server endpoint (spec I): ``POST /mcp``.

Streamable-HTTP MCP, stateless, JSON responses only. Distinct from the
``mcp`` router, which manages SCF's *outbound* connections to other
people's servers (ADR-0006: separate trust directions).

Order of checks: authentication (401, via ``get_current_principal``) → Origin
(403) → body size (413) → JSON-RPC parsing → current-revision metadata and
header validation (400) → method dispatch. Every ``tools/call`` writes exactly
one audit event carrying a SHA-256 of the arguments, never the arguments
themselves.

Current-revision requests (spec 007 FR-001 to FR-005) need no ``initialize``:
``server/discover``, ``tools/list`` and ``tools/call`` answer with
``resultType`` and the server's identity in ``_meta``; anything else is HTTP 404.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from scf_api import __version__
from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.mcp_server.protocol import (
    INTERNAL_ERROR,
    META_SERVER_INFO,
    METHOD_NOT_FOUND,
    SUPPORTED_VERSIONS,
    JsonRpcError,
    Message,
    failure,
    modern_version,
    negotiate_version,
    parse_message,
    success,
)
from scf_api.mcp_server.registry import ToolContext, ToolRegistry
from scf_api.observability.audit import AuditCategory, AuditLog, compute_payload_hash
from scf_api.settings import Settings, get_settings

#: Largest accepted request body (spec I §4).
MAX_BODY_BYTES = 256 * 1024

INSTRUCTIONS = (
    "Swarm Code Factory is a software factory. Its world model answers two questions with two "
    "timestamps: `at` is when something was true in the world, `as_of` is what SCF "
    "had recorded by then; both default to now. Facts carry evidence. Write tools only "
    "create proposals that a person must accept."
)
SERVER_INFO = {"name": "forge", "version": __version__}
CAPABILITIES = {"tools": {"listChanged": False}}
#: ``private``: ``tools/list`` is filtered by the caller's role, so a cache shared
#: across tokens would leak which write tools exist.
CACHE_HINTS = {"ttlMs": 60_000, "cacheScope": "private"}

router = APIRouter(tags=["mcp-server"])
logger = logging.getLogger(__name__)


@functools.cache
def get_mcp_tool_registry() -> ToolRegistry:
    """The tools this server exposes (spec I §5)."""
    from scf_api.mcp_server.context_tools import CONTEXT_TOOLS
    from scf_api.mcp_server.factory_tools import FACTORY_TOOLS
    from scf_api.mcp_server.knowledge_tools import KNOWLEDGE_TOOLS
    from scf_api.mcp_server.world_tools import WORLD_TOOLS

    return ToolRegistry((*WORLD_TOOLS, *FACTORY_TOOLS, *CONTEXT_TOOLS, *KNOWLEDGE_TOOLS))


@functools.cache
def get_mcp_server_audit_log() -> AuditLog:
    from scf_api.observability.audit_db import default_audit_log

    return default_audit_log()


PrincipalDep = Annotated[Principal, Depends(get_current_principal)]
SessionDep = Annotated[Session, Depends(get_db)]
RegistryDep = Annotated[ToolRegistry, Depends(get_mcp_tool_registry)]
AuditDep = Annotated[AuditLog, Depends(get_mcp_server_audit_log)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _dispatch(
    message: Message,
    *,
    modern: bool,
    principal: Principal,
    session: Session,
    registry: ToolRegistry,
    audit: AuditLog,
    overrides: Mapping[Any, Callable[[], Any]],
) -> dict[str, Any]:
    method = message.method
    if modern and method == "server/discover":
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": CAPABILITIES,
            "instructions": INSTRUCTIONS,
            **CACHE_HINTS,
        }
    if not modern and method == "initialize":
        return {
            "protocolVersion": negotiate_version(message.params.get("protocolVersion")),
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if not modern and method == "ping":
        return {}
    if method == "tools/list":
        tools = {"tools": registry.list_payload(principal)}
        return {**tools, **CACHE_HINTS} if modern else tools
    if method == "tools/call":
        name = message.params.get("name")
        arguments = message.params.get("arguments")
        started = time.monotonic()
        outcome = "error"
        try:
            result = registry.call(ToolContext(principal, session, overrides), name, arguments)
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
    raise JsonRpcError(
        METHOD_NOT_FOUND, f"method not found: {method}", http_status=404 if modern else 200
    )


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
        modern = modern_version(request.headers, message) is not None
        result = await run_in_threadpool(
            _dispatch,
            message,
            modern=modern,
            principal=principal,
            session=session,
            registry=registry,
            audit=audit,
            overrides=request.app.dependency_overrides,
        )
    except JsonRpcError as exc:
        return JSONResponse(
            failure(message.request_id, exc.code, exc.message, exc.data), exc.http_status
        )
    except Exception:  # never leak internals over the wire
        logger.exception("mcp server: %s failed", message.method)
        return JSONResponse(failure(message.request_id, INTERNAL_ERROR, "internal error"))
    if modern:
        result = {**result, "resultType": "complete", "_meta": {META_SERVER_INFO: SERVER_INFO}}
    return JSONResponse(success(message.request_id, result))


@router.get("/mcp")
def mcp_get(_: PrincipalDep) -> Response:
    """No server-initiated stream in this phase (spec I §3)."""
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED, headers={"Allow": "POST"})


__all__ = ["MAX_BODY_BYTES", "get_mcp_server_audit_log", "get_mcp_tool_registry", "router"]
