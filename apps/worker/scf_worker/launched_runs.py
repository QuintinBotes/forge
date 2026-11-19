"""Runs handed to a non-inline executor: launch, close-out and reconciliation (#239).

With a non-inline ``FORGE_RUN_EXECUTOR``, ``forge.agent.run`` prepares the run in the
worker and launches one unit for it (:func:`launch_run`), recording the executor and
unit identifier under ``agent_run.output["executor"]``, next to the handoff the unit
records (#238), and returns without waiting. Close-out (:func:`close_out_launched_run`,
task ``agent.close_out_run``) delivers, records the outcome and writes back from that
handoff, then removes the unit. The reconciler (``agent.reconcile_launched_runs``, on
beat) closes out exited units, fails a run whose unit is lost with ``executor_lost``,
and stops a unit past the wall-clock limit plus a grace period, escalating its run.

Close-out happens once (FR-018): it is claimed under the run's row lock, and a run
whose claim is taken is never closed out again, whoever triggers it.
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

from sqlalchemy import select

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
from scf_worker.run_executor import RunExecutor, UnitState, build_executor
from scf_worker.run_main import (
    DEFAULT_RUN_TIME_LIMIT,
    RUN_PROCESS_ALLOWED_ENV,
    RUN_TIME_LIMIT_ENV,
    existing_mirror,
)
from scf_worker.run_record import EXECUTOR_KEY, HANDOFF_KEY
from scf_worker.tasks.audit import AUDIT_RECORD_TASK

__all__ = [
    "CLOSE_OUT_TASK",
    "EXECUTOR_LOST",
    "RECONCILE_GRACE_ENV",
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
RECONCILE_GRACE_ENV = "FORGE_RUN_RECONCILE_GRACE_SECONDS"
DEFAULT_RECONCILE_GRACE = 300.0
EXECUTOR_LOST = "executor_lost"
WALL_CLOCK_EXCEEDED = "wall_clock_limit_exceeded"


def unit_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A unit's whole environment: only the names a run process may hold (FR-011).

    The one place per-unit credentials join it (the run-scoped token is a
    follow-up); the database credential is never one of them.
    """
    source = os.environ if env is None else env
    return {name: value for name, value in source.items() if name in RUN_PROCESS_ALLOWED_ENV}


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

    ``None`` when the run is not launchable. A failed launch takes close-out with it,
    for the caller to perform.
    """
    with create_session_factory()() as session:
        row = session.get(AgentRun, run.run_id, with_for_update=True)
        if row is None or not _launchable(row, run.workspace_id):
            return None
        record: dict[str, Any] = {"name": executor.name, "unit": None, "launched_at": _now()}
        try:
            executor.preflight()
            record["unit"] = executor.launch(run.run_id, run.workspace_id, env=unit_environment())
        except Exception as exc:
            reason = SecretRedactor().redact(f"{type(exc).__name__}: {exc}")
            record["error"] = f"the {executor.name} run executor could not launch the run: {reason}"
            record["closed_out_at"] = record["launched_at"]
        row.output = {**(row.output or {}), EXECUTOR_KEY: record}
        session.commit()
        return record


def close_out_launched_run(
    run_id: uuid.UUID, workspace_id: uuid.UUID, *, result: AgentRunResult | None = None
) -> dict[str, Any]:
    """Close a launched run out once, from its handoff or from the reconciler's ``result``.

    Without either it does nothing: whether the unit was lost is the reconciler's call.
    """
    claimed = _claim_close_out(run_id, workspace_id, need_handoff=result is None)
    if claimed is None:
        return {"closed_out": False, "run_id": str(run_id)}
    record, inputs, handoff = claimed
    objective = AgentObjective.model_validate(inputs)
    context = {**objective.context, "agent_run_id": str(run_id), "workspace_id": str(workspace_id)}
    objective = objective.model_copy(update={"context": context})
    with contextlib.suppress(RepositoryUnavailable):
        objective = existing_mirror(objective)  # delivery pushes the branch the unit left there
    run = PreparedRun(objective, run_id, workspace_id, True, _event_sink(run_id, workspace_id))
    status = _finish(run, result or AgentRunResult.model_validate(handoff), record)
    return {"closed_out": True, "run_id": str(run_id), "status": status}


def _claim_close_out(
    run_id: uuid.UUID, workspace_id: uuid.UUID, *, need_handoff: bool
) -> tuple[dict[str, Any], dict[str, Any], Any] | None:
    """Take close-out under the row lock, as ``_record_run_outcome`` does; ``None`` if not ours."""
    with create_session_factory()() as session:
        row = session.get(AgentRun, run_id, with_for_update=True)
        if row is None or row.workspace_id != workspace_id or row.completed_at is not None:
            return None
        output = dict(row.output or {})
        record = output.get(EXECUTOR_KEY)
        if not record or record.get("closed_out_at"):
            return None
        if need_handoff and HANDOFF_KEY not in output:
            return None
        record = {**record, "closed_out_at": _now()}
        row.output = {**output, EXECUTOR_KEY: record}
        session.commit()
        return record, dict(row.inputs or {}), output.get(HANDOFF_KEY)


def _finish(run: PreparedRun, result: AgentRunResult, record: dict[str, Any]) -> str:
    """The claimed close-out: delivery, terminal events, outcome and write-back; then the unit."""
    try:
        close_out_run(run, result)
        status = result.status.value
    except Exception:  # close_out_run recorded the run failed before raising
        logger.exception("close-out of run %s failed", run.run_id)
        status = RunStatus.FAILED.value
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
    limit = timedelta(
        seconds=float(os.environ.get(RUN_TIME_LIMIT_ENV) or DEFAULT_RUN_TIME_LIMIT)
        + float(os.environ.get(RECONCILE_GRACE_ENV) or DEFAULT_RECONCILE_GRACE)
    )
    with create_session_factory()() as session:
        # shortcut: filters every run not completed in Python; a JSON index if that set grows.
        open_runs = select(AgentRun.id, AgentRun.workspace_id, AgentRun.output).where(
            AgentRun.completed_at.is_(None)
        )
        launched = []
        for run_id, workspace_id, output in session.execute(open_runs):
            record = (output or {}).get(EXECUTOR_KEY)
            if record and not record.get("closed_out_at"):
                launched.append((run_id, workspace_id, record, HANDOFF_KEY in output))
    closed: Counter[str] = Counter()
    for run_id, workspace_id, record, handed_off in launched:
        try:
            reason = _reconcile(run_id, workspace_id, record, handed_off, now - limit)
        except Exception:  # an unreachable runtime decides nothing; the next pass retries
            logger.exception("could not reconcile launched run %s", run_id)
            continue
        if reason is not None:
            closed[reason] += 1
    return dict(closed)


def _reconcile(
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    record: dict[str, Any],
    handed_off: bool,
    overdue_before: datetime,
) -> str | None:
    executor = build_executor(record["name"])
    unit: str | None = record.get("unit")
    state = executor.state(unit) if unit else UnitState("missing")
    result = None
    if unit and state.phase == "running":
        if datetime.fromisoformat(record["launched_at"]) >= overdue_before:
            return None
        executor.remove(unit)  # stopped before its run is recorded
        result = AgentRunResult(
            run_id=run_id, status=RunStatus.ESCALATED, error=WALL_CLOCK_EXCEEDED
        )
    elif not handed_off:
        result = AgentRunResult(run_id=run_id, status=RunStatus.FAILED, error=EXECUTOR_LOST)
    if not close_out_launched_run(run_id, workspace_id, result=result)["closed_out"]:
        return None
    return result.error if result else "handoff"


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
            result="error" if outcome == RunStatus.FAILED.value else "success",
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
