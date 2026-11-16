"""A task's held-out acceptance scenarios: create, list, approve, delete (#176).

Every query is filtered on the caller's workspace. A scenario is a draft until a
person approves it, and the identity that drafted it cannot. Each change is
chained into the audit log with who made it; the files and the command are not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from scf_api.deps import Principal
from scf_api.schemas.held_out import HeldOutScenarioCreate
from scf_contracts import UserRole
from scf_contracts.audit import AuditEvent
from scf_db.audit.writer import SqlAuditWriter
from scf_db.models import Task
from scf_db.models.held_out import SCENARIO_APPROVED, SCENARIO_DRAFT, HeldOutScenario

__all__ = [
    "ApprovalRefused",
    "ScenarioNotFound",
    "approve_scenario",
    "create_scenario",
    "delete_scenario",
    "list_scenarios",
]


class ScenarioNotFound(LookupError):
    """No such task or scenario in the caller's workspace."""


class ApprovalRefused(PermissionError):
    """Only a person who did not draft the scenario may approve it."""


def _identity(principal: Principal) -> str:
    return f"user:{principal.user_id}" if principal.decides else f"key:{principal.credential_id}"


def _task(session: Session, principal: Principal, task_id: uuid.UUID) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.workspace_id != principal.workspace_id:
        raise ScenarioNotFound(str(task_id))
    return task


def _scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> HeldOutScenario:
    row = session.get(HeldOutScenario, scenario_id)
    if row is None or row.workspace_id != principal.workspace_id or row.task_id != task_id:
        raise ScenarioNotFound(str(scenario_id))
    return row


def _audit(session: Session, principal: Principal, action: str, row: HeldOutScenario) -> None:
    SqlAuditWriter(session).emit(
        AuditEvent(
            workspace_id=principal.workspace_id,
            action=action,
            actor_id=principal.user_id,
            actor_type="user" if principal.decides else "system",
            target_type="held_out_scenario",
            scope_type="task",
            scope_id=row.task_id,
            severity="notice",
            detail_ref={"table": "held_out_scenario", "id": str(row.id)},
            details={"scenario_id": str(row.id), "task_id": str(row.task_id)},
        )
    )


def create_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, body: HeldOutScenarioCreate
) -> HeldOutScenario:
    """Store a draft scenario for the task, recording who drafted it."""
    _task(session, principal, task_id)
    row = HeldOutScenario(
        id=uuid.uuid4(),
        workspace_id=principal.workspace_id,
        task_id=task_id,
        title=body.title,
        files=[f.model_dump() for f in body.files],
        command=body.command,
        drafted_by=_identity(principal),
        drafted_by_user_id=principal.user_id,
        state=SCENARIO_DRAFT,
    )
    session.add(row)
    session.flush()
    _audit(session, principal, "held_out.created", row)
    return row


def list_scenarios(
    session: Session, principal: Principal, task_id: uuid.UUID
) -> list[HeldOutScenario]:
    _task(session, principal, task_id)
    return list(
        session.scalars(
            select(HeldOutScenario)
            .where(
                HeldOutScenario.workspace_id == principal.workspace_id,
                HeldOutScenario.task_id == task_id,
            )
            .order_by(HeldOutScenario.created_at, HeldOutScenario.id)
        )
    )


def approve_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> HeldOutScenario:
    """Approve a draft. Refused for an agent or any non-person credential, and for its drafter."""
    row = _scenario(session, principal, task_id, scenario_id)
    if not principal.decides or principal.role is UserRole.AGENT_RUNNER:
        raise ApprovalRefused("only a person can approve a held-out scenario")
    if principal.user_id == row.drafted_by_user_id or _identity(principal) == row.drafted_by:
        raise ApprovalRefused("the identity that drafted a scenario cannot approve it")
    if row.state != SCENARIO_APPROVED:
        row.state = SCENARIO_APPROVED
        row.approved_by = principal.user_id
        row.approved_at = datetime.now(UTC)
        session.flush()
        _audit(session, principal, "held_out.approved", row)
    return row


def delete_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> None:
    row = _scenario(session, principal, task_id, scenario_id)
    _audit(session, principal, "held_out.deleted", row)
    session.delete(row)
    session.flush()
