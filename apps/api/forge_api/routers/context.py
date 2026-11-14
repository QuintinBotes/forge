"""Context assembly REST surface, mounted at ``/context`` (spec J §2.1).

``POST /context/assemble`` returns one token-budgeted bundle for a task.
Reads need READ; naming an ``agent_run_id`` additionally writes the run's
``context_refs``, which needs WRITE or RUN_AGENT. A run outside the
caller's workspace is 404. The heavy lifting lives in
:mod:`forge_api.services.context_service`; the MCP tool of the same name
calls this handler so both answers cannot drift.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from forge_api.auth.rbac import Permission, can
from forge_api.deps import DbSession, Principal, get_current_principal
from forge_api.routers._rbac import require_permission
from forge_api.routers.knowledge import get_knowledge_service
from forge_api.routers.spec import get_spec_engine
from forge_api.schemas.context import AssembleIn, AssembleOut
from forge_api.services.context_service import (
    AgentRunNotFoundError,
    RunFinishedError,
    assemble_context,
)
from forge_api.services.world_service import get_world_service
from forge_knowledge import KnowledgeService
from forge_spec import FileSpecEngine
from forge_world import WorldModelService

router = APIRouter(
    prefix="/context",
    tags=["context"],
    dependencies=[Depends(get_current_principal)],
)

ReaderDep = Annotated[Principal, Depends(require_permission(Permission.READ))]
WorldDep = Annotated[WorldModelService, Depends(get_world_service)]
KnowledgeDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]
EngineDep = Annotated[FileSpecEngine, Depends(get_spec_engine)]


@router.post("/assemble", response_model=AssembleOut)
def assemble(
    principal: ReaderDep,
    session: DbSession,
    world: WorldDep,
    knowledge: KnowledgeDep,
    engine: EngineDep,
    body: AssembleIn,
) -> AssembleOut:
    """Assemble the bundle; with an ``agent_run_id``, record its refs."""
    if body.agent_run_id is not None and not (
        can(principal.role, Permission.WRITE) or can(principal.role, Permission.RUN_AGENT)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"role '{principal.role.value}' lacks permission 'write' or 'run_agent'",
        )
    try:
        bundle = assemble_context(
            session=session,
            world=world,
            knowledge=knowledge,
            spec_engine=engine,
            workspace_id=principal.workspace_id,
            task=body.task,
            budget_tokens=body.budget_tokens,
            template=body.template,
            agent_run_id=body.agent_run_id,
            entities=body.entities,
            spec_ids=body.spec_ids,
        )
    except AgentRunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RunFinishedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if body.agent_run_id is not None:
        session.commit()
    return bundle


__all__ = ["router"]
