"""Context assembly over MCP (spec J §2.1).

``assemble_context`` calls the same handler the REST route serves, so the
MCP and REST bundles cannot drift; an HTTP error from the handler comes
back as an in-band tool error. The tool is listed for readers, but naming
an ``agent_run_id`` writes that run's ``context_refs`` — the handler
itself refuses that for principals without WRITE or RUN_AGENT.
"""

from __future__ import annotations

from typing import Any

from forge_api.mcp_server.registry import McpTool, ToolContext
from forge_api.mcp_server.world_tools import _call
from forge_api.routers import context as context_router
from forge_api.routers import spec as spec_router
from forge_api.routers.knowledge import get_knowledge_service
from forge_api.schemas.context import AssembleIn, AssembleOut
from forge_api.services.world_service import make_world_service


def _spec_engine(ctx: ToolContext) -> Any:
    override = ctx.overrides.get(spec_router.get_spec_engine)
    if override is not None:
        return override()
    return ctx.provide(spec_router.get_spec_registry).for_workspace(ctx.principal.workspace_id)


def _assemble(ctx: ToolContext, args: AssembleIn) -> AssembleOut:
    return _call(
        lambda: context_router.assemble(
            principal=ctx.principal,
            session=ctx.session,
            world=make_world_service(ctx.session),
            knowledge=ctx.provide(get_knowledge_service),
            engine=_spec_engine(ctx),
            body=args,
        )
    )


CONTEXT_TOOLS: tuple[McpTool, ...] = (
    McpTool(
        "assemble_context",
        "One token-budgeted context bundle for a task: world facts with evidence and "
        "conflict markers, knowledge passages (with the honest legs/vector_leg envelope), "
        "spec requirements with their status, memories by effective confidence, and repo "
        "instruction files. With `agent_run_id`, records the bundle's refs on that run.",
        AssembleIn,
        _assemble,
        # The bundle is already token-budgeted (and requirement blocks are
        # atomic), so the registry must not cut item text a second time.
        max_text_chars=None,
    ),
)

__all__ = ["CONTEXT_TOOLS"]
