"""A task's held-out acceptance scenarios: create, list, decide, delete (#176, #288).

Every query is filtered on the caller's workspace. A scenario is a draft until a
person approves or rejects it, and the identity that drafted it cannot. One the
verifier drafted for a run's spec is withdrawn when that spec goes back for
changes, and the run's ``spec`` gate cannot be approved while one is undecided.
Each change is chained into the audit log with who made it and the run; the
files and the command are not. So is each scenario's run on a change (#286): its
outcome by scenario id, never its content or output, read back by id and title.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.orm import Session

from scf_api.deps import Principal
from scf_api.schemas.held_out import HeldOutResultOut, HeldOutScenarioCreate
from scf_api.services.approval_service import to_decider
from scf_contracts import UserRole
from scf_contracts.audit import AuditEvent
from scf_db.audit.writer import SqlAuditWriter
from scf_db.models import Task
from scf_db.models.audit import AuditLog
from scf_db.models.held_out import (
    SCENARIO_APPROVED,
    SCENARIO_DRAFT,
    SCENARIO_REJECTED,
    SCENARIO_WITHDRAWN,
    HeldOutScenario,
)

__all__ = [
    "RESULT_ACTION",
    "ApprovalRefused",
    "ScenarioNotFound",
    "ScenarioNotOpen",
    "ScenariosUndecided",
    "approve_scenario",
    "create_scenario",
    "delete_scenario",
    "drafted_for_run",
    "ensure_scenarios_decided",
    "list_results",
    "list_scenarios",
    "record_result",
    "reject_scenario",
    "scenarios_for_run",
    "store_agent_draft",
    "store_draft",
    "withdraw_run_scenarios",
]

#: The audit action recording one scenario's run on a change (#286).
RESULT_ACTION = "held_out.ran"


class ScenarioNotFound(LookupError):
    """No such task or scenario in the caller's workspace."""


class ApprovalRefused(PermissionError):
    """Only a person who did not draft the scenario may approve or reject it."""


class ScenarioNotOpen(ValueError):
    """Only a draft is decided; a decided or withdrawn scenario keeps its state."""


class ScenariosUndecided(Exception):
    """The run's ``spec`` gate cannot be approved while a scenario drafted for it is a draft."""

    def __init__(self, scenario_ids: list[uuid.UUID]) -> None:
        super().__init__(
            f"{len(scenario_ids)} held-out scenario(s) are neither approved nor rejected"
        )
        self.scenario_ids = scenario_ids


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


def _emit(
    session: Session,
    row: HeldOutScenario,
    action: str,
    *,
    actor_id: uuid.UUID | None,
    actor_type: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Chain ``action`` on ``row`` into the audit log: who, never the content."""
    SqlAuditWriter(session).emit(
        AuditEvent(
            workspace_id=row.workspace_id,
            action=action,
            actor_id=actor_id,
            actor_type=actor_type,
            target_type="held_out_scenario",
            scope_type="task",
            scope_id=row.task_id,
            severity="notice",
            detail_ref={"table": "held_out_scenario", "id": str(row.id)},
            details={
                "scenario_id": str(row.id),
                "task_id": str(row.task_id),
                "workflow_run_id": str(row.workflow_run_id) if row.workflow_run_id else None,
                **(extra or {}),
            },
        )
    )


def _audit(session: Session, principal: Principal, action: str, row: HeldOutScenario) -> None:
    _emit(
        session,
        row,
        action,
        actor_id=principal.user_id,
        actor_type="user" if principal.decides else "system",
        # Which credential and surface acted, as approval decisions record.
        extra={
            "key_id": str(principal.credential_id) if principal.credential_id else None,
            "surface": principal.surface,
        },
    )


def store_draft(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    body: HeldOutScenarioCreate,
    drafted_by: str,
    drafted_by_user_id: uuid.UUID | None,
    workflow_run_id: uuid.UUID | None = None,
) -> HeldOutScenario:
    """Add a draft scenario to the store; the caller audits its creation.

    The one place a scenario row is built, for the API and for the verifier (#258).
    """
    row = HeldOutScenario(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        title=body.title,
        files=[f.model_dump() for f in body.files],
        command=body.command,
        drafted_by=drafted_by,
        drafted_by_user_id=drafted_by_user_id,
        state=SCENARIO_DRAFT,
    )
    session.add(row)
    session.flush()
    return row


def store_agent_draft(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    body: HeldOutScenarioCreate,
    drafted_by: str,
    agent_run_id: uuid.UUID,
    workflow_run_id: uuid.UUID,
) -> HeldOutScenario:
    """Store a draft an agent wrote for a run's spec (the verifier, #258), audited as its."""
    row = store_draft(
        session,
        workspace_id=workspace_id,
        task_id=task_id,
        body=body,
        drafted_by=drafted_by,
        drafted_by_user_id=None,
        workflow_run_id=workflow_run_id,
    )
    _emit(
        session,
        row,
        "held_out.created",
        actor_id=None,
        actor_type="agent",
        extra={"agent_run_id": str(agent_run_id)},
    )
    return row


def create_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, body: HeldOutScenarioCreate
) -> HeldOutScenario:
    """Store a draft scenario for the task, recording who drafted it."""
    _task(session, principal, task_id)
    row = store_draft(
        session,
        workspace_id=principal.workspace_id,
        task_id=task_id,
        body=body,
        drafted_by=_identity(principal),
        drafted_by_user_id=principal.user_id,
    )
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


def _decide(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID, state: str
) -> HeldOutScenario:
    """Approve or reject a draft. Refused for an agent or any non-person credential, and
    for its drafter; a decided or withdrawn scenario keeps its state.

    The caller is resolved as approval decisions are (``to_decider``): a key decides
    with the lower of its own role and its owner's current role.
    """
    row = _scenario(session, principal, task_id, scenario_id)
    decider = to_decider(principal, session)
    if (
        not principal.decides
        or decider.kind != "user"
        or decider.role not in {UserRole.MEMBER, UserRole.ADMIN}
    ):
        raise ApprovalRefused("only a person with write access can decide a held-out scenario")
    if principal.user_id == row.drafted_by_user_id or _identity(principal) == row.drafted_by:
        raise ApprovalRefused("the identity that drafted a scenario cannot decide it")
    session.refresh(row, with_for_update=True)  # a concurrent withdrawal is seen, not undone
    if row.state == state:  # the same decision again changes nothing
        return row
    if row.state != SCENARIO_DRAFT:
        raise ScenarioNotOpen(f"the scenario is {row.state}; only a draft is decided")
    row.state = state
    if state == SCENARIO_APPROVED:
        row.approved_by = principal.user_id
        row.approved_at = datetime.now(UTC)
    session.flush()
    _audit(session, principal, f"held_out.{state}", row)
    return row


def approve_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> HeldOutScenario:
    """A person's approval: only approved scenarios are run."""
    return _decide(session, principal, task_id, scenario_id, SCENARIO_APPROVED)


def reject_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> HeldOutScenario:
    """A person's rejection (#288): the scenario is never run."""
    return _decide(session, principal, task_id, scenario_id, SCENARIO_REJECTED)


def delete_scenario(
    session: Session, principal: Principal, task_id: uuid.UUID, scenario_id: uuid.UUID
) -> None:
    row = _scenario(session, principal, task_id, scenario_id)
    if row.workflow_run_id is not None:  # deleting would pass the spec gate undecided
        raise ScenarioNotOpen("a scenario drafted for a run is decided or withdrawn, not deleted")
    _audit(session, principal, "held_out.deleted", row)
    session.delete(row)
    session.flush()


def drafted_for_run(
    session: Session, *, workspace_id: uuid.UUID, workflow_run_id: uuid.UUID
) -> list[HeldOutScenario]:
    """The scenarios drafted for the run's current spec: every one not withdrawn."""
    return list(
        session.scalars(
            select(HeldOutScenario)
            .where(
                HeldOutScenario.workspace_id == workspace_id,
                HeldOutScenario.workflow_run_id == workflow_run_id,
                HeldOutScenario.state != SCENARIO_WITHDRAWN,
            )
            .order_by(HeldOutScenario.created_at, HeldOutScenario.id)
        )
    )


def ensure_scenarios_decided(
    session: Session, *, workspace_id: uuid.UUID, workflow_run_id: uuid.UUID
) -> None:
    """Refuse the run's ``spec`` approval while a scenario drafted for it is undecided.

    A closed default (ADR-0007): an undecided scenario is not silently dropped.
    """
    rows = drafted_for_run(session, workspace_id=workspace_id, workflow_run_id=workflow_run_id)
    undecided = [row.id for row in rows if row.state == SCENARIO_DRAFT]
    if undecided:
        raise ScenariosUndecided(undecided)


def withdraw_run_scenarios(
    session: Session, *, workspace_id: uuid.UUID, workflow_run_id: uuid.UUID
) -> list[HeldOutScenario]:
    """Withdraw what was drafted for the run's earlier spec, whatever its state (#288)."""
    rows = drafted_for_run(session, workspace_id=workspace_id, workflow_run_id=workflow_run_id)
    for row in rows:
        row.state = SCENARIO_WITHDRAWN
        _emit(session, row, "held_out.withdrawn", actor_id=None, actor_type="system")
    session.flush()
    return rows


def scenarios_for_run(
    session: Session, *, workspace_id: uuid.UUID, task_id: uuid.UUID, workflow_run_id: uuid.UUID
) -> list[HeldOutScenario]:
    """The scenarios that apply to a run of the task (#286): every one a person wrote
    for the task and every one the verifier drafted for this run, not withdrawn.

    One drafted for another run of the task was decided against that run's spec,
    so it does not apply; nor does a verifier's draft stored before migration 0051,
    which names no run but was drafted for one. Only the approved ones are run.
    """
    return list(
        session.scalars(
            select(HeldOutScenario)
            .where(
                HeldOutScenario.workspace_id == workspace_id,
                HeldOutScenario.task_id == task_id,
                or_(
                    and_(
                        HeldOutScenario.workflow_run_id.is_(None),
                        not_(HeldOutScenario.drafted_by.startswith("agent:")),
                    ),
                    HeldOutScenario.workflow_run_id == workflow_run_id,
                ),
                HeldOutScenario.state != SCENARIO_WITHDRAWN,
            )
            .order_by(HeldOutScenario.created_at, HeldOutScenario.id)
        )
    )


def record_result(
    session: Session,
    row: HeldOutScenario,
    *,
    workflow_run_id: uuid.UUID,
    attempt: int,
    head_commit: str,
    outcome: Mapping[str, Any],
) -> None:
    """Chain one scenario's run on ``head_commit`` into the audit log (#286).

    ``outcome`` is ``passed``, ``exit_code``, ``timed_out``, ``duration_ms`` and
    ``reason``: never the files, the command or the output. ``workflow_run_id``
    names the run it ran in, not the one it was drafted for.
    """
    _emit(
        session,
        row,
        RESULT_ACTION,
        actor_id=None,
        actor_type="system",
        extra={
            **outcome,
            "workflow_run_id": str(workflow_run_id),
            "attempt": attempt,
            "head_commit": head_commit,
        },
    )


def list_results(
    session: Session,
    principal: Principal,
    task_id: uuid.UUID,
    workflow_run_id: uuid.UUID | None = None,
) -> list[HeldOutResultOut]:
    """Each scenario run on the task's changes, oldest first, by id and title only."""
    _task(session, principal, task_id)
    titles = dict(
        session.execute(
            select(HeldOutScenario.id, HeldOutScenario.title).where(
                HeldOutScenario.workspace_id == principal.workspace_id,
                HeldOutScenario.task_id == task_id,
            )
        ).all()
    )
    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.workspace_id == principal.workspace_id,
            AuditLog.action == RESULT_ACTION,
            AuditLog.scope_type == "task",
            AuditLog.scope_id == task_id,
        )
        .order_by(AuditLog.seq)
    )
    results = [
        HeldOutResultOut.model_validate(
            {
                **row.details,
                "title": titles.get(uuid.UUID(row.details["scenario_id"])),
                "recorded_at": row.created_at,
            }
        )
        for row in rows
    ]
    if workflow_run_id is None:
        return results
    return [r for r in results if r.workflow_run_id == workflow_run_id]
