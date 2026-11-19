"""A task's held-out acceptance scenarios and their results on its runs (#176, #286).

Write access only: a reader without it gets 403, and a task or scenario in
another workspace is a 404. Approval and rejection are a person's decision, never
an agent's and never the drafter's; only a draft is decided (409 otherwise). No
agent-facing surface reads these scenarios or their results.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from scf_api.auth.rbac import Permission
from scf_api.db import get_db
from scf_api.deps import Principal, get_current_principal
from scf_api.routers._rbac import require_permission
from scf_api.schemas.held_out import (
    HeldOutResultOut,
    HeldOutScenarioCreate,
    HeldOutScenarioOut,
)
from scf_api.services.held_out import (
    ApprovalRefused,
    ScenarioNotFound,
    ScenarioNotOpen,
    approve_scenario,
    create_scenario,
    delete_scenario,
    list_results,
    list_scenarios,
    reject_scenario,
)

router = APIRouter(
    prefix="/tasks/{task_id}/held-out-scenarios",
    tags=["held-out-scenarios"],
    dependencies=[Depends(get_current_principal)],
)

WriterDep = Annotated[Principal, Depends(require_permission(Permission.WRITE))]
SessionDep = Annotated[Session, Depends(get_db)]


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except ScenarioNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such task or scenario") from None
    except ApprovalRefused as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except ScenarioNotOpen as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.post("", response_model=HeldOutScenarioOut, status_code=status.HTTP_201_CREATED)
def create(
    principal: WriterDep, session: SessionDep, task_id: uuid.UUID, body: HeldOutScenarioCreate
) -> HeldOutScenarioOut:
    """Store a draft scenario; who drafted it is recorded."""
    with _errors():
        row = create_scenario(session, principal, task_id, body)
    session.commit()
    return HeldOutScenarioOut.model_validate(row)


@router.get("", response_model=list[HeldOutScenarioOut])
def list_(
    principal: WriterDep, session: SessionDep, task_id: uuid.UUID
) -> list[HeldOutScenarioOut]:
    with _errors():
        rows = list_scenarios(session, principal, task_id)
    return [HeldOutScenarioOut.model_validate(row) for row in rows]


@router.get("/results", response_model=list[HeldOutResultOut])
def results(
    principal: WriterDep,
    session: SessionDep,
    task_id: uuid.UUID,
    workflow_run_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[HeldOutResultOut]:
    """Each scenario's run on the task's changes, by id and title, oldest first."""
    with _errors():
        return list_results(session, principal, task_id, workflow_run_id)


@router.post("/{scenario_id}/approve", response_model=HeldOutScenarioOut)
def approve(
    principal: WriterDep, session: SessionDep, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> HeldOutScenarioOut:
    """A person's approval, recorded with who and when. Only approved scenarios run."""
    with _errors():
        row = approve_scenario(session, principal, task_id, scenario_id)
    session.commit()
    return HeldOutScenarioOut.model_validate(row)


@router.post("/{scenario_id}/reject", response_model=HeldOutScenarioOut)
def reject(
    principal: WriterDep, session: SessionDep, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> HeldOutScenarioOut:
    """A person's rejection, chained into the audit log. A rejected scenario never runs."""
    with _errors():
        row = reject_scenario(session, principal, task_id, scenario_id)
    session.commit()
    return HeldOutScenarioOut.model_validate(row)


@router.delete("/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    principal: WriterDep, session: SessionDep, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> Response:
    with _errors():
        delete_scenario(session, principal, task_id, scenario_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
