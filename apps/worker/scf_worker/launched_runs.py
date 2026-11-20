"""Runs handed to a non-inline executor: launch, close-out and reconciliation (#239).

With a non-inline ``FORGE_RUN_EXECUTOR``, ``forge.agent.run`` prepares the run in the
worker and launches one unit for it (:func:`launch_run`), recording the executor and
unit identifier under ``agent_run.output["executor"]``, next to the handoff the unit
records (#238), and returns without waiting. Close-out (:func:`close_out_launched_run`,
task ``agent.close_out_run``) delivers, records the outcome and writes back from that
handoff, then removes the unit. The reconciler (``agent.reconcile_launched_runs``, on
beat) closes out exited units, fails a run whose unit is lost with ``executor_lost``,
and stops a unit past the wall-clock limit plus a grace period, escalating its run.

Close-out happens once (FR-018): it is claimed under the run's row lock and is done
once the outcome is recorded. A claim is a lease, not a final mark: one whose worker
was killed, or whose outcome was not recorded, expires after the hard task time
limit and the reconciler takes it again. A retry delivers to the pull request the
first attempt may have opened; terminal events a killed attempt already stored can
then appear twice.
"""

from __future__ import annotations

import contextlib
import logging
import os
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from scf_api.auth.run_token import issue_run_token, revoke_run_token
from scf_api.observability.redaction import redact_text
from scf_auth.redaction import SecretRedactor
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_contracts.audit import AuditEvent
from scf_db.models import AgentRun
from scf_db.session import create_session_factory
from scf_worker.agent_runner import (
    PreparedRun,
    _event_sink,
    _opt_uuid,
    close_out_run,
    prepare_run,
)
from scf_worker.beat import RUN_RECONCILE_TASK
from scf_worker.celery_app import celery_app
from scf_worker.dispatch import RepositoryUnavailable
from scf_worker.run_executor import RunExecutor, build_executor, run_deadline_seconds
from scf_worker.run_main import RUN_PROCESS_ALLOWED_ENV, RUN_TOKEN_ENV, existing_mirror
from scf_worker.run_record import EXECUTOR_KEY, HANDOFF_KEY
from scf_worker.tasks.audit import AUDIT_RECORD_TASK

__all__ = [
    "CLOSE_OUT_TASK",
    "EXECUTOR_LOST",
    "EXECUTOR_UNREACHABLE",
    "RUN_RECORD_UNREADABLE",
    "WALL_CLOCK_EXCEEDED",
    "close_out_launched_run",
    "close_out_task",
    "launch_run",
    "reconcile_launched_runs",
    "reconcile_task",
    "unit_environment",
]

logger = logging.getLogger("scf_worker.launched_runs")

CLOSE_OUT_TASK = "agent.close_out_run"
EXECUTOR_LOST = "executor_lost"
WALL_CLOCK_EXCEEDED = "wall_clock_limit_exceeded"
EXECUTOR_UNREACHABLE = "executor_unreachable: the unit may still exist"
RUN_RECORD_UNREADABLE = "run_record_unreadable"


def unit_environment(run_token: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A unit's whole environment: only the names a run process may hold (FR-011).

    The one place per-unit credentials join it: the token of the unit's own run
    (#257). The database credential is never one of them.
    """
    source = os.environ if env is None else env
    allowed = {name: value for name, value in source.items() if name in RUN_PROCESS_ALLOWED_ENV}
    return {**allowed, RUN_TOKEN_ENV: run_token}


def launch_run(parsed: AgentObjective, executor_name: str) -> dict[str, Any]:
    """Prepare ``parsed`` in the worker, launch one unit for it and return at once.

    A run already launched or closed out launches nothing. A failed preflight or
    launch fails the run naming the executor; the loop never runs here instead (FR-021).
    """
    run_id = _opt_uuid(parsed.context.get("agent_run_id"))
    workspace_id = _opt_uuid(parsed.context.get("workspace_id")) or uuid.UUID(int=0)
    if run_id is None:
        # A unit reads its objective from its run's record, and this run has none.
        return AgentRunResult(
            task_id=parsed.task_id,
            status=RunStatus.FAILED,
            error=f"the {executor_name} run executor needs a stored run",
        ).model_dump(mode="json")
    not_launched = {"launched": False, "run_id": str(run_id)}
    with create_session_factory()() as session:
        if not _launchable(session.get(AgentRun, run_id), workspace_id):
            return not_launched
    run, refused = prepare_run(parsed)
    if refused is not None:
        close_out_run(run, refused)
        return refused.model_dump(mode="json")
    record = _launch_once(run, build_executor(executor_name))
    if record is None:
        return not_launched
    _audit("agent_run.launched", run, record, "launched" if record["unit"] else "failed")
    if record["unit"] is None:
        failed = AgentRunResult(
            run_id=run_id, task_id=parsed.task_id, status=RunStatus.FAILED, error=record["error"]
        )
        _finish(run, failed, record)
        return failed.model_dump(mode="json")
    return {"launched": True, "run_id": str(run_id), **record}


def _launchable(row: AgentRun | None, workspace_id: uuid.UUID) -> bool:
    return (
        row is not None
        and row.workspace_id == workspace_id
        and row.completed_at is None
        and EXECUTOR_KEY not in (row.output or {})
    )


def _launch_once(run: PreparedRun, executor: RunExecutor) -> dict[str, Any] | None:
    """Launch under the run's row lock, so a concurrent duplicate waits, then sees the record.

    The unit gets its run's token, which must be stored before the unit starts: issuing
    commits, so the record is first stored as a claim (the close-out lease), which keeps
    a duplicate delivery and the reconciler off the run until the unit is recorded.
    ``None`` when the run is not launchable. A failed launch takes close-out with it,
    for the caller to perform.
    """
    with create_session_factory()() as session:
        row = session.get(AgentRun, run.run_id, with_for_update=True)
        if row is None or not _launchable(row, run.workspace_id):
            return None
        launched_at = _now()
        record: dict[str, Any] = {"name": executor.name, "unit": None, "launched_at": launched_at}
        try:
            executor.preflight()
            claim = {**record, "closing_since": launched_at}
            row.output = {**(row.output or {}), EXECUTOR_KEY: claim}
            token = issue_run_token(
                session,
                run_id=run.run_id,
                workspace_id=run.workspace_id,
                run_deadline_seconds=run_deadline_seconds(),
            ).token
            record["unit"] = executor.launch(
                run.run_id, run.workspace_id, env=unit_environment(token)
            )
        except Exception as exc:
            session.rollback()  # a failed flush or commit while issuing leaves it unusable
            # Both redactors: a launch error may quote the unit's environment, token included.
            reason = redact_text(SecretRedactor().redact(f"{type(exc).__name__}: {exc}"))
            record["error"] = f"the {executor.name} run executor could not launch the run: {reason}"
            record["closing_since"] = launched_at
        # Read afresh under the lock again: the unit may have recorded its handoff by now,
        # and after a rollback another delivery may have taken the run.
        row = session.get(AgentRun, run.run_id, with_for_update=True, populate_existing=True)
        stored = (row.output or {}).get(EXECUTOR_KEY) if row is not None else None
        if row is None or not (
            _launchable(row, run.workspace_id) or (stored or {}).get("launched_at") == launched_at
        ):
            return None
        row.output = {**(row.output or {}), EXECUTOR_KEY: record}
        session.commit()
        return record


def close_out_launched_run(
    run_id: uuid.UUID, workspace_id: uuid.UUID, *, verdict: AgentRunResult | None = None
) -> dict[str, Any]:
    """Close a launched run out once: from its handoff, else from the reconciler's ``verdict``.

    Without either it does nothing: whether the unit was lost is the reconciler's call.
    """
    claimed = _claim_close_out(run_id, workspace_id, verdict)
    if claimed is None:
        return {"closed_out": False, "run_id": str(run_id)}
    record, objective, result, reason = claimed
    run = PreparedRun(objective, run_id, workspace_id, True, _event_sink(run_id, workspace_id))
    status = _finish(run, result, record)
    if status is None:
        return {"closed_out": False, "run_id": str(run_id)}
    return {"closed_out": True, "run_id": str(run_id), "status": status, "reason": reason}


def _close_out_lease() -> timedelta:
    """How long a claim holds: past the hard task time limit Celery has killed its worker."""
    return timedelta(seconds=int(celery_app.conf.task_time_limit or 360) + 60)


def _claim_held(record: dict[str, Any], now: datetime) -> bool:
    since = record.get("closing_since")
    return since is not None and now - datetime.fromisoformat(since) < _close_out_lease()


def _claim_close_out(
    run_id: uuid.UUID, workspace_id: uuid.UUID, verdict: AgentRunResult | None = None
) -> tuple[dict[str, Any], AgentObjective, AgentRunResult, str] | None:
    """Take close-out under the row lock, as ``_record_run_outcome`` does; ``None`` if not ours.

    Decided from the locked row: a handoff recorded by now wins over any verdict, and
    an unreadable run record fails the run rather than stranding it.
    """
    with create_session_factory()() as session:
        row = session.get(AgentRun, run_id, with_for_update=True)
        if row is None or row.workspace_id != workspace_id or row.completed_at is not None:
            return None
        output = dict(row.output or {})
        record = output.get(EXECUTOR_KEY)
        if not record or _claim_held(record, datetime.now(UTC)):
            return None
        if HANDOFF_KEY in output:
            reason, source = "handoff", output[HANDOFF_KEY]
        elif verdict is not None:
            reason, source = str(verdict.error), verdict.model_dump(mode="json")
        else:
            return None
        try:
            objective = AgentObjective.model_validate(row.inputs or {})
            result = AgentRunResult.model_validate(source)
        except ValidationError:
            reason = RUN_RECORD_UNREADABLE
            objective = AgentObjective(objective="")
            result = AgentRunResult(run_id=run_id, status=RunStatus.FAILED, error=reason)
        record = {**record, "closing_since": _now()}
        row.output = {**output, EXECUTOR_KEY: record}
        session.commit()
    identity = {"agent_run_id": str(run_id), "workspace_id": str(workspace_id)}
    objective = objective.model_copy(update={"context": {**objective.context, **identity}})
    with contextlib.suppress(RepositoryUnavailable):
        objective = existing_mirror(objective)  # delivery pushes the branch the unit left there
    return record, objective, result, reason


def _finish(run: PreparedRun, result: AgentRunResult, record: dict[str, Any]) -> str | None:
    """The claimed close-out: delivery, terminal events, outcome and write-back; then the unit.

    ``None`` when no outcome was recorded: the claim then expires and is retried.
    """
    try:  # the unit's record closes first: its token is refused from here on
        with create_session_factory()() as session:
            revoke_run_token(session, run_id=run.run_id, workspace_id=run.workspace_id)
    except Exception:  # recording the outcome removes the token too
        logger.exception("could not revoke the run process's access for run %s", run.run_id)
    try:
        close_out_run(run, result, reuse_existing=True)
    except Exception:  # recorded as failed before raising, unless that write failed too
        logger.exception("close-out of run %s failed", run.run_id)
    with create_session_factory()() as session:
        row = session.get(AgentRun, run.run_id)
        status = row.status.value if row is not None and row.completed_at is not None else None
    if status is None:
        logger.error("run %s recorded no outcome; its close-out is retried", run.run_id)
        return None
    if record.get("unit"):
        try:
            build_executor(record["name"]).remove(record["unit"])
        except Exception:
            logger.exception("could not remove unit %s of run %s", record["unit"], run.run_id)
    _audit("agent_run.closed_out", run, record, status)
    return status


def reconcile_launched_runs(*, now: datetime | None = None) -> dict[str, int]:
    """One pass over launched runs not closed out; how many it closed out, by reason (FR-019)."""
    now = now or datetime.now(UTC)
    overdue_before = now - timedelta(seconds=run_deadline_seconds())
    with create_session_factory()() as session:
        # shortcut: filters every run not completed in Python; a JSON index if that set grows.
        open_runs = select(AgentRun.id, AgentRun.workspace_id, AgentRun.output).where(
            AgentRun.completed_at.is_(None)
        )
        launched = []
        for run_id, workspace_id, output in session.execute(open_runs):
            record = (output or {}).get(EXECUTOR_KEY)
            if record and not _claim_held(record, now):
                launched.append((run_id, workspace_id, record))
    closed: Counter[str] = Counter()
    for run_id, workspace_id, record in launched:
        try:
            reason = _reconcile(run_id, workspace_id, record, overdue_before)
        except Exception:  # an unreachable runtime decides nothing yet; the next pass retries
            logger.exception("could not reconcile launched run %s", run_id)
            continue
        if reason is not None:
            closed[reason] += 1
    return dict(closed)


def _reconcile(
    run_id: uuid.UUID, workspace_id: uuid.UUID, record: dict[str, Any], overdue_before: datetime
) -> str | None:
    """Close the run out if its unit is done or overdue; a handoff wins under the lock."""
    unit: str | None = record.get("unit")
    overdue = datetime.fromisoformat(record["launched_at"]) < overdue_before
    status, error = RunStatus.FAILED, EXECUTOR_LOST
    if unit is None:  # a failed launch whose close-out did not finish
        error = record.get("error") or EXECUTOR_LOST
    else:
        executor = build_executor(record["name"])
        try:
            phase: str = executor.state(unit).phase
        except Exception:
            if not overdue:
                raise  # an unreachable runtime decides nothing before the deadline
            phase = "unreachable"
        if phase == "running":
            if not overdue:
                return None
            executor.remove(unit)  # stopped before its run is recorded
            status, error = RunStatus.ESCALATED, WALL_CLOCK_EXCEEDED
        elif phase == "unreachable":
            status, error = RunStatus.ESCALATED, EXECUTOR_UNREACHABLE
    verdict = AgentRunResult(run_id=run_id, status=status, error=error)
    closed = close_out_launched_run(run_id, workspace_id, verdict=verdict)
    return closed["reason"] if closed["closed_out"] else None


def _audit(action: str, run: PreparedRun, record: dict[str, Any], outcome: str) -> None:
    """One event on the audit chain through ``audit.record`` (FR-023); never fails the run.

    The executor, unit and outcome only: never the unit's environment.
    """
    try:
        event = AuditEvent(
            workspace_id=run.workspace_id,
            action=action,
            actor_type="system",
            actor_label="system:run_executor",
            target_type="agent_run",
            target_id=run.run_id,
            result="success" if outcome in ("launched", RunStatus.SUCCEEDED.value) else "error",
            details={"executor": record["name"], "unit": record.get("unit"), "outcome": outcome},
        )
        celery_app.send_task(AUDIT_RECORD_TASK, args=[event.model_dump(mode="json")])
    except Exception:
        logger.exception("could not send the %s audit event of run %s", action, run.run_id)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@celery_app.task(name=CLOSE_OUT_TASK)
def close_out_task(run_id: str, workspace_id: str) -> dict[str, Any]:
    """Close a launched run out from its handoff; safe to trigger more than once."""
    return close_out_launched_run(uuid.UUID(run_id), uuid.UUID(workspace_id))


@celery_app.task(name=RUN_RECONCILE_TASK)
def reconcile_task() -> dict[str, int]:
    """Beat entrypoint: one reconciliation pass over launched runs."""
    return reconcile_launched_runs()
