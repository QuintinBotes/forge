"""Forge's factory records over MCP, for desktop clients and IDEs (spec L seam 1).

Each tool calls the handler its REST route serves, so the two answers cannot drift;
a domain error raised as HTTP comes back as an in-band tool error. All five are
reads. Every result carries the provenance fields of ADR-0015: forge observed this
record, when, and that reading it enforces nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import anyio.from_thread
from pydantic import BaseModel, Field

from forge_api.mcp_server.registry import McpTool, ToolContext
from forge_api.mcp_server.world_tools import MAX_LIMIT, _call
from forge_api.routers import agent as agent_router
from forge_api.routers import approvals as approvals_router
from forge_api.routers import board as board_router
from forge_api.routers import spec as spec_router
from forge_api.services.approval_service import get_approval_service
from forge_approval.models import GateStatus
from forge_contracts import RunStatus


class Observed(BaseModel):
    """A record as forge observed it (ADR-0015 provenance)."""

    observed_by: str = "forge"
    recorded_at: datetime
    enforceable: bool = False
    result: Any


def _observed(result: Any) -> Observed:
    return Observed(recorded_at=datetime.now(UTC), result=result)


def _run_async[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run an async handler on the app's event loop from the MCP worker thread."""
    return anyio.from_thread.run(factory)


def _spec_engine(ctx: ToolContext) -> Any:
    override = ctx.overrides.get(spec_router.get_spec_engine)
    if override is not None:
        return override()
    return ctx.provide(spec_router.get_spec_registry).for_workspace(ctx.principal.workspace_id)


def _board(ctx: ToolContext) -> Any:
    override = ctx.overrides.get(board_router.get_board_service)
    if override is not None:
        return override()
    registry = ctx.provide(board_router.get_board_registry)
    return board_router.get_board_service(principal=ctx.principal, registry=registry)


class SpecListIn(BaseModel):
    project_id: uuid.UUID


class SpecGetIn(BaseModel):
    spec_id: str = Field(description="Spec uuid or key, e.g. SPEC-1")


class BoardItemsIn(BaseModel):
    project_id: uuid.UUID | None = None
    status: list[str] | None = Field(default=None, description="Status names to include")
    text: str | None = Field(default=None, description="Text to search titles for")
    limit: int = Field(default=50, ge=1, le=MAX_LIMIT)


class ApprovalsPendingIn(BaseModel):
    project_id: uuid.UUID | None = None


class RunStatusIn(BaseModel):
    run_id: uuid.UUID


class RunStatusOut(BaseModel):
    run_id: uuid.UUID | None
    status: RunStatus
    summary: str | None
    error: str | None
    confidence: float | None
    needs_human: bool
    steps: int
    changed_files: list[str]
    artifacts: dict[str, Any]


def _spec_list(ctx: ToolContext, args: SpecListIn) -> BaseModel:
    dashboard = _call(
        lambda: spec_router.project_spec_overview(
            project_id=args.project_id,
            principal=ctx.principal,
            engine=_spec_engine(ctx),
            board=_board(ctx),
            db=ctx.session,
        )
    )
    return _observed(dashboard)


def _spec_get(ctx: ToolContext, args: SpecGetIn) -> BaseModel:
    spec_id = _call(lambda: spec_router.resolve_spec_ref(args.spec_id))
    manifest = _call(lambda: spec_router.read_manifest(engine=_spec_engine(ctx), spec_id=spec_id))
    return _observed(manifest)


def _board_items(ctx: ToolContext, args: BoardItemsIn) -> BaseModel:
    tasks = _call(
        lambda: board_router.list_tasks(
            svc=_board(ctx),
            project_id=args.project_id,
            status=args.status,
            kind=None,
            priority=None,
            label=None,
            assignee_id=None,
            sprint_id=None,
            epic_id=None,
            text=args.text,
            limit=args.limit,
            offset=0,
        )
    )
    return _observed({"items": tasks})


def _approvals_pending(ctx: ToolContext, args: ApprovalsPendingIn) -> BaseModel:
    service = ctx.provide(get_approval_service)
    pending = _call(
        lambda: _run_async(
            lambda: approvals_router.list_approvals(
                service=service,
                principal=ctx.principal,
                status_filter=GateStatus.PENDING,
                gate_type=None,
                project_id=args.project_id,
                mine=False,
            )
        )
    )
    return _observed({"items": pending})


def _run_status(ctx: ToolContext, args: RunStatusIn) -> BaseModel:
    override = ctx.overrides.get(agent_router.get_agent_store)
    store = override() if override is not None else agent_router.AgentRunStore(ctx.session)
    run = _call(
        lambda: agent_router.get_run(store=store, principal=ctx.principal, run_id=args.run_id)
    )
    return _observed(
        RunStatusOut(
            run_id=run.run_id,
            status=run.status,
            summary=run.summary,
            error=run.error,
            confidence=run.confidence,
            needs_human=run.needs_human,
            steps=len(run.steps),
            changed_files=run.changed_files,
            artifacts=run.artifacts,
        )
    )


FACTORY_TOOLS: tuple[McpTool, ...] = (
    McpTool(
        "spec_list",
        "A project's specs with their latest validation, and its constitution.",
        SpecListIn,
        _spec_list,
    ),
    McpTool("spec_get", "One spec's manifest, by uuid or key (e.g. SPEC-1).", SpecGetIn, _spec_get),
    McpTool(
        "board_items",
        "Board tasks, optionally filtered by project, status or title text.",
        BoardItemsIn,
        _board_items,
    ),
    McpTool(
        "approvals_pending",
        "Approval requests still waiting for a decision, highest risk first.",
        ApprovalsPendingIn,
        _approvals_pending,
    ),
    McpTool(
        "run_status",
        "An agent run's status, summary, changed files and artifacts (e.g. its pull request).",
        RunStatusIn,
        _run_status,
    ),
)

__all__ = ["FACTORY_TOOLS"]
