"""Issue-driven dispatch + run write-back (spec M M3/M2).

Two halves, both driven from data SCF already persists:

* :func:`maybe_dispatch` — given one verified, deduped GitHub Issues webhook
  delivery plus the adapter's parsed hint subset (label / sender / repository),
  decide *fail-closed* (ADR-0007) whether a label event may start an agent
  run, and if so create + enqueue it through the same code path as
  ``POST /agent/runs`` (:func:`scf_api.services.agent_dispatch.create_and_enqueue_run`).
  The objective is built from the issue *re-fetched through the adapter*,
  never from the webhook payload (ADR-0014). With the Temporal backend
  selected the label starts a governed workflow run for the issue's board task
  instead, which walks every gate, the spec gate first (#171). Every refusal is returned as a
  reason for the caller to record on the delivery — never an error to GitHub.
  ``forge.pm.process_webhook`` calls this inline (single owner of the delivery
  row); the ``forge.pm.dispatch_issue`` task below is the standalone re-drive
  seam for the same logic.

* :func:`post_run_writeback` — when a run that carries a tracker ref reaches a
  terminal state, post exactly one run-state comment (state, PR link if any,
  hidden ``<!-- forge:run:<run_id> -->`` marker) to the originating issue and,
  only when a PR was opened, apply the connection's mapped
  ``status_on_pr_opened`` transition. Best-effort: failures are logged and
  recorded on the run row, never raised into the run's outcome.

Triggers beyond ``issues.labeled`` (status transitions, Linear/Jira) are out
of scope for M3 — see spec M §4 "Next".
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import or_, select

from scf_api.observability.audit import AuditCategory, AuditLog
from scf_api.schemas.pm import PMDispatchConfig
from scf_api.services.agent_dispatch import (
    AgentEnqueueFailed,
    RepoNotConnected,
    UngovernedRunRefused,
    build_tracker_objective,
    create_and_enqueue_run,
    repo_connected,
)
from scf_api.services.pm_service import PMConnectionService
from scf_contracts import AgentObjective, AgentRunResult, RunStatus
from scf_db.models import AgentRun
from scf_db.models.enums import PMConnectionStatus, PMProvider, PMSyncDirection
from scf_db.models.enums import RunStatus as DbRunStatus
from scf_db.models.pm import PMConnection, PMTaskLink, PMWebhookDelivery
from scf_db.models.runs import WorkflowRun as DbWorkflowRun
from scf_integrations.github import GitHubClient
from scf_integrations.pm.sync_engine import run_comment_marker
from scf_worker.celery_app import celery_app
from scf_worker.reliability import ScfTask as ScfReliableTask

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

#: Standalone re-drive task for one delivery's dispatch decision. Registered
#: when this module is imported — which ``scf_worker.tasks.pm_sync`` (in the
#: Celery ``include`` list) does — mirroring ``register_pm_sync_tasks``.
PM_DISPATCH_TASK = "forge.pm.dispatch_issue"

_ACTIVE_RUN_STATUSES = (DbRunStatus.PENDING, DbRunStatus.RUNNING)
#: A workflow run waiting at a gate is still active (it holds its issue's slot).
_ACTIVE_WORKFLOW_STATUSES = (*_ACTIVE_RUN_STATUSES, DbRunStatus.ESCALATED)


@dataclass(frozen=True)
class DispatchOutcome:
    """One dispatch evaluation: ``not_applicable`` | ``skipped`` | ``dispatched``."""

    action: str
    reason: str | None = None
    run_id: str | None = None


def _default_run_enqueue(objective: AgentObjective) -> None:  # pragma: no cover - prod seam
    from scf_api.routers.agent import enqueue_agent_run

    enqueue_agent_run(objective)


def _parse_dispatch_config(connection: PMConnection) -> PMDispatchConfig | None:
    try:
        return PMDispatchConfig.model_validate((connection.config or {}).get("dispatch") or {})
    except ValidationError:
        return None


def _aware(value: datetime | None) -> datetime:
    """Normalise a (possibly naive, e.g. SQLite-read) timestamp to aware UTC."""
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hard_time_limit_s() -> int:
    """The worker's hard task time limit (Celery config), with a safe fallback."""
    try:
        return int(celery_app.conf.task_time_limit or 360)
    except (TypeError, ValueError):  # pragma: no cover - misconfigured conf
        return 360


def _dispatch_guard(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    external_id: str,
    delivery_id: str,
    payload_hash: str | None = None,
    max_active_runs: int,
    now: datetime | None = None,
) -> str | None:
    """A skip reason when an idempotency / concurrency guard trips, else None.

    Guards, in order: this very delivery — or another delivery with the same
    signed body (``payload_hash``) — already produced a run (a re-queued or
    replayed task can never enqueue twice); another run for the same issue is
    still queued/running; the connection is at its ``max_active_runs`` cap.

    Only the scalars the guard needs are selected (status plus the tracker
    ``connection_id`` / ``external_id`` / ``delivery_id`` / ``payload_hash``
    read through JSON paths), restricted to rows that are active or created
    in the last 7 days — ``steps``/``output`` are never loaded.

    A PENDING/RUNNING row counts as active while it is queued (no
    ``started_at``, created within the last day) or within the hard task time
    limit plus 10 minutes of the worker starting it, so a run whose worker
    died without recording an outcome cannot block its issue and cap slot
    forever, and a run still waiting in the broker keeps counting.
    """
    now = _aware(now) if now is not None else datetime.now(UTC)
    recent_cutoff = now - timedelta(days=7)
    # shortcut: this age bound stands in for a stale-run reaper that should
    # terminally close dead PENDING/RUNNING runs; replace the bound with it.
    started_cutoff = now - timedelta(seconds=_hard_time_limit_s() + 600)
    queued_cutoff = now - timedelta(days=1)
    tracker_connection = AgentRun.inputs[("context", "tracker", "connection_id")].as_string()
    tracker_external = AgentRun.inputs[("context", "tracker", "external_id")].as_string()
    tracker_delivery = AgentRun.inputs[("context", "tracker", "delivery_id")].as_string()
    tracker_hash = AgentRun.inputs[("context", "tracker", "payload_hash")].as_string()
    rows = session.execute(
        select(
            AgentRun.status,
            AgentRun.created_at,
            AgentRun.started_at,
            tracker_external,
            tracker_delivery,
            tracker_hash,
        ).where(
            AgentRun.workspace_id == workspace_id,
            tracker_connection == str(connection_id),
            or_(
                AgentRun.status.in_(_ACTIVE_RUN_STATUSES),
                AgentRun.created_at >= recent_cutoff,
            ),
        )
    ).all()
    active_for_connection = 0
    for status, created_at, started_at, run_external, run_delivery, run_hash in rows:
        if run_delivery == delivery_id or (payload_hash and run_hash == payload_hash):
            return "already_dispatched"
        alive = (
            _aware(started_at) > started_cutoff
            if started_at is not None
            else _aware(created_at) > queued_cutoff
        )
        if status in _ACTIVE_RUN_STATUSES and alive:
            if run_external == external_id:
                return "issue_run_active"
            active_for_connection += 1
    if active_for_connection >= max_active_runs:
        return "max_active_runs_reached"
    return None


def maybe_dispatch(
    session_factory: sessionmaker[Session],
    *,
    connection: PMConnection,
    delivery: PMWebhookDelivery,
    payload: dict | None,
    connections: PMConnectionService,
    enqueue_run: Callable[[AgentObjective], None] | None = None,
    audit: AuditLog | None = None,
) -> DispatchOutcome:
    """Evaluate one delivery as a dispatch trigger; create the run if allowed.

    Fail-closed: anything but an enabled, fully-configured, allow-listed
    ``issues.labeled`` event on the connection's own repository is a skip with
    a reason. Adapter fetch failures propagate (the caller's retry/terminal
    semantics apply); every other refusal is data, not an exception.
    """
    if connection.provider != PMProvider.GITHUB_ISSUES:
        return DispatchOutcome("not_applicable")
    if not payload or payload.get("event") != "issues" or payload.get("action") != "labeled":
        return DispatchOutcome("not_applicable")

    def skipped(reason: str) -> DispatchOutcome:
        if audit is not None:
            audit.record(
                category=AuditCategory.SYSTEM,
                action="pm_dispatch_skipped",
                workspace_id=connection.workspace_id,
                connection_id=str(connection.id),
                target=delivery.delivery_id,
                status=reason,
            )
        return DispatchOutcome("skipped", reason=reason)

    config = _parse_dispatch_config(connection)
    if config is None:
        return skipped("invalid_dispatch_config")
    if not config.enabled:
        return skipped("dispatch_disabled")
    if payload.get("is_pull_request"):
        return skipped("pull_request")
    payload_repo = payload.get("repository_full_name")
    if (
        not isinstance(payload_repo, str)
        or payload_repo.lower() != GitHubClient.owner_repo(connection.external_project_key).lower()
    ):
        return skipped("repository_mismatch")
    if payload.get("label") != config.label:
        return skipped("label_mismatch")
    if not config.repository:
        return skipped("repository_not_configured")
    if delivery.external_id is None:
        return skipped("missing_external_id")
    sender_login = payload.get("sender_login")
    if not config.sender_allowed(sender_login if isinstance(sender_login, str) else None):
        return skipped("sender_not_allowed")

    with session_factory() as session:
        if not repo_connected(session, connection.workspace_id, config.repository):
            return skipped("repository_not_connected")
        guard = _dispatch_guard(
            session,
            workspace_id=connection.workspace_id,
            connection_id=connection.id,
            external_id=delivery.external_id,
            delivery_id=delivery.delivery_id,
            payload_hash=delivery.payload_hash,
            max_active_runs=config.max_active_runs,
        )
        if guard is not None:
            return skipped(guard)
        link = session.scalar(
            select(PMTaskLink).where(
                PMTaskLink.connection_id == connection.id,
                PMTaskLink.external_id == delivery.external_id,
            )
        )
        task_id = link.forge_task_id if link is not None else None

    adapter = connections.get_adapter_for_project(
        connection.workspace_id, connection.project_id, connection.provider.value
    )
    external = asyncio.run(adapter.fetch_external(delivery.external_id))
    objective, server_context = build_tracker_objective(
        external,
        connection_id=connection.id,
        repository=config.repository,
        actor=f"pm:{connection.id}",
        sender_login=sender_login if isinstance(sender_login, str) else None,
        delivery_id=delivery.delivery_id,
        payload_hash=delivery.payload_hash,
        task_id=task_id,
    )
    if _durable_engine_selected():
        return _dispatch_governed(
            session_factory,
            connection=connection,
            delivery=delivery,
            connections=connections,
            external=external,
            objective=objective,
            server_context=server_context,
            task_id=task_id,
            sender_login=sender_login if isinstance(sender_login, str) else None,
            max_active_runs=config.max_active_runs,
            skipped=skipped,
            audit=audit,
        )
    with session_factory() as session:
        # Serialise dispatch per connection: the fetch above sits between the
        # first guard and the insert, so without this row lock two concurrent
        # deliveries could both pass the guard. The guard re-runs under the
        # lock in the session that creates the run; create_and_enqueue_run's
        # commit (row before broker enqueue) releases it. SQLite ignores
        # FOR UPDATE, so the hermetic tests are unaffected.
        session.execute(
            select(PMConnection.id).where(PMConnection.id == connection.id).with_for_update()
        )
        guard = _dispatch_guard(
            session,
            workspace_id=connection.workspace_id,
            connection_id=connection.id,
            external_id=delivery.external_id,
            delivery_id=delivery.delivery_id,
            payload_hash=delivery.payload_hash,
            max_active_runs=config.max_active_runs,
        )
        if guard is not None:
            return skipped(guard)
        try:
            result = create_and_enqueue_run(
                session,
                workspace_id=connection.workspace_id,
                objective=objective,
                enqueue=enqueue_run or _default_run_enqueue,
                server_context=server_context,
            )
        except RepoNotConnected:
            return skipped("repository_not_connected")
        except UngovernedRunRefused:
            # Outside development an issue never runs without a workflow run (#277).
            return skipped("ungoverned_run_refused")
        except AgentEnqueueFailed as exc:
            # The persisted run is already terminal FAILED; the person who
            # added the label still gets its run-state comment.
            try:
                failed_run_id = uuid.UUID(str(exc.args[0]))
            except (IndexError, ValueError):  # pragma: no cover - defensive
                failed_run_id = None
            if failed_run_id is not None:
                post_run_writeback(
                    session_factory,
                    run_id=failed_run_id,
                    workspace_id=connection.workspace_id,
                    result=AgentRunResult(
                        run_id=failed_run_id,
                        task_id=task_id,
                        status=RunStatus.FAILED,
                        error="enqueue_failed",
                    ),
                    tracker=server_context["tracker"],
                    connections=connections,
                )
            return skipped("enqueue_failed")
    if audit is not None:
        audit.record(
            category=AuditCategory.SYSTEM,
            action="pm_dispatch",
            workspace_id=connection.workspace_id,
            connection_id=str(connection.id),
            target=delivery.delivery_id,
            status="dispatched",
            metadata={"run_id": str(result.run_id), "external_id": delivery.external_id},
        )
    return DispatchOutcome("dispatched", run_id=str(result.run_id))


def _durable_engine_selected() -> bool:
    from scf_workflow.temporal.config import TemporalSettings

    return TemporalSettings().workflow_engine_backend == "temporal"


def _workflow_run_guard(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    delivery_id: str,
    payload_hash: str | None,
    max_active_runs: int,
) -> str | None:
    """The dispatch guard for governed runs, over the workflow runs issues started.

    This delivery, or another with the same signed body, already started a run;
    or the connection is at its ``max_active_runs`` cap. One active run per issue
    is the engine's own rule (one active Temporal run per task).
    """
    tracker = ("trigger", "tracker")
    run_connection = DbWorkflowRun.context[(*tracker, "connection_id")].as_string()
    run_delivery = DbWorkflowRun.context[(*tracker, "delivery_id")].as_string()
    run_hash = DbWorkflowRun.context[(*tracker, "payload_hash")].as_string()
    rows = session.execute(
        select(DbWorkflowRun.status, run_delivery, run_hash).where(
            DbWorkflowRun.workspace_id == workspace_id,
            run_connection == str(connection_id),
        )
    ).all()
    active = 0
    for status, delivered, hashed in rows:
        if delivered == delivery_id or (payload_hash and hashed == payload_hash):
            return "already_dispatched"
        if status in _ACTIVE_WORKFLOW_STATUSES:
            active += 1
    if active >= max_active_runs:
        return "max_active_runs_reached"
    return None


def _give_task_the_repository(
    session_factory: sessionmaker[Session], task_id: uuid.UUID, objective: AgentObjective
) -> str | None:
    """Make the run's board task name the repository dispatch acts on.

    Every step of a governed run (the guard, the agent, the checks, the pull
    request) reads the task's repository, and a task the board sync created from
    the issue names none: it gets the one the dispatch config names. A task that
    already names another repository is refused rather than run against two.
    """
    from scf_api.services.agent_dispatch import claim_task_repository
    from scf_db.models import Task

    if objective.primary_repo_target is None:
        return None
    with session_factory() as session:
        task = session.get(Task, task_id)
        if task is None:
            return "no_board_task"
        if not claim_task_repository(task, objective):
            return "task_repository_mismatch"
        session.commit()
    return None


def _dispatch_governed(
    session_factory: sessionmaker[Session],
    *,
    connection: PMConnection,
    delivery: PMWebhookDelivery,
    connections: PMConnectionService,
    external: Any,
    objective: AgentObjective,
    server_context: dict[str, Any],
    task_id: uuid.UUID | None,
    sender_login: str | None,
    max_active_runs: int,
    skipped: Callable[[str], DispatchOutcome],
    audit: AuditLog | None,
) -> DispatchOutcome:
    """Start a governed workflow run for the issue's board task (#171).

    The label only starts the run: the workflow walks every gate, the spec gate
    first, and its agent runs the objective kept on the run (built from the
    re-fetched issue). The sender is recorded as the run's starter, never as an
    approver. Tracker write-back for governed runs is #278.
    """
    from scf_api.services.workflow_engine import select_workflow_engine
    from scf_workflow.exceptions import DuplicateRunError
    from scf_workflow.temporal.engine import TemporalWorkflowEngine

    if task_id is None:
        # No board task linked yet: create it (and its link), as the board sync does.
        from scf_worker.tasks.pm_sync import sync_external_task

        task_id = sync_external_task(session_factory, connections, connection, external, audit)
        if task_id is None:
            return skipped("no_board_task")
    mismatch = _give_task_the_repository(session_factory, task_id, objective)
    if mismatch is not None:
        return skipped(mismatch)
    starter = f"github:{sender_login}" if sender_login else f"pm:{connection.id}"
    trigger: dict[str, object] = {
        "source": "issue",
        "started_by": starter,
        "tracker": server_context["tracker"],
        # The agent renders the issue's text as untrusted data under this label.
        "source_label": server_context.get("source"),
        "objective": objective.model_copy(update={"task_id": task_id}).model_dump(mode="json"),
    }
    with session_factory() as session:
        # Serialised per connection, as the direct path is (see maybe_dispatch).
        session.execute(
            select(PMConnection.id).where(PMConnection.id == connection.id).with_for_update()
        )
        assert delivery.external_id is not None  # guarded by maybe_dispatch
        guard = _workflow_run_guard(
            session,
            workspace_id=connection.workspace_id,
            connection_id=connection.id,
            delivery_id=delivery.delivery_id,
            payload_hash=delivery.payload_hash,
            max_active_runs=max_active_runs,
        )
        if guard is not None:
            return skipped(guard)
        engine = select_workflow_engine(session=session, workspace_id=connection.workspace_id)
        assert isinstance(engine, TemporalWorkflowEngine)  # the durable engine is selected
        try:
            run = engine.start(task_id, trigger=trigger, started_by=starter)
        except DuplicateRunError:
            return skipped("issue_run_active")
        except Exception:  # the engine recorded the failed start; never the direct path (#277)
            logger.exception("the durable engine could not start a run for task %s", task_id)
            return skipped("durable_engine_unavailable")
        session.commit()
    if audit is not None:
        audit.record(
            category=AuditCategory.SYSTEM,
            action="pm_dispatch",
            workspace_id=connection.workspace_id,
            connection_id=str(connection.id),
            target=delivery.delivery_id,
            status="dispatched",
            metadata={"workflow_run_id": str(run.id), "external_id": delivery.external_id},
        )
    return DispatchOutcome("dispatched", run_id=str(run.id))


# --------------------------------------------------------------------------- #
# Run write-back (spec M M2)                                                  #
# --------------------------------------------------------------------------- #


def _record_writeback(
    session_factory: sessionmaker[Session], run_id: uuid.UUID, record: dict
) -> None:
    with session_factory() as session:
        row = session.get(AgentRun, run_id)
        if row is None:  # pragma: no cover - row deleted mid-flight
            return
        row.output = {**(row.output or {}), "tracker_writeback": record}
        session.commit()


def _claim_writeback(session_factory: sessionmaker[Session], run_id: uuid.UUID) -> bool:
    """Claim the one write-back for ``run_id``; False when already claimed/done.

    Sets ``output.tracker_writeback = {"status": "posting"}`` in one
    transaction only when no record exists yet, so concurrent or redelivered
    write-backs post at most once. A missing row cannot be claimed; proceed
    (legacy best-effort) rather than suppress the comment.
    """
    with session_factory() as session:
        row = session.get(AgentRun, run_id, with_for_update=True)
        if row is None:  # pragma: no cover - row deleted mid-flight
            return True
        if (row.output or {}).get("tracker_writeback"):
            return False
        row.output = {**(row.output or {}), "tracker_writeback": {"status": "posting"}}
        session.commit()
        return True


def post_run_writeback(
    session_factory: sessionmaker[Session],
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    result: AgentRunResult,
    tracker: dict,
    connections: PMConnectionService,
) -> str:
    """Post the one run-state comment for a tracker-born run; never raises.

    Returns ``"posted"``, ``"already_recorded"``, ``"skipped"``,
    ``"unsupported"`` or ``"failed"``. The write-back is claimed on the run
    row (``output.tracker_writeback = {"status": "posting"}``) before
    anything is posted, then the outcome replaces the claim — at most one
    comment, without touching the run's recorded status. A disabled or
    inbound-only connection is skipped with the reason recorded.
    ``status_on_pr_opened`` is applied only when the run actually opened a
    pull request.
    """
    try:
        if not _claim_writeback(session_factory, run_id):
            return "already_recorded"
        connection_id = uuid.UUID(str(tracker["connection_id"]))
        external_id = str(tracker["external_id"])
        connection = connections.get_connection_any_workspace(connection_id)
        if connection is None or connection.workspace_id != workspace_id:
            raise LookupError(f"tracker connection {connection_id} not in run workspace")
        if connection.status == PMConnectionStatus.DISABLED:
            _record_writeback(
                session_factory,
                run_id,
                {"status": "skipped", "reason": "connection_disabled"},
            )
            return "skipped"
        if connection.sync_direction == PMSyncDirection.INBOUND_ONLY:
            _record_writeback(
                session_factory, run_id, {"status": "skipped", "reason": "inbound_only"}
            )
            return "skipped"
        adapter: Any = connections.get_adapter_for_project(
            connection.workspace_id, connection.project_id, connection.provider.value
        )
        comment = getattr(adapter, "comment_external", None)
        if comment is None:
            _record_writeback(session_factory, run_id, {"status": "unsupported"})
            return "unsupported"

        pr = (result.artifacts or {}).get("pull_request") or {}
        pr_opened = pr.get("status") == "opened" and bool(pr.get("url"))
        lines = [f"SCF run `{run_id}` finished with state **{result.status.value}**."]
        if pr_opened:
            lines.append(f"Pull request: {pr['url']}")
        lines.append(run_comment_marker(run_id))
        body = "\n\n".join(lines)
        config = _parse_dispatch_config(connection) if pr_opened else None
        category = config.status_on_pr_opened if config is not None else None

        # One event loop for comment + transition: the production adapter's
        # HTTP client is bound to the loop that first used it, so a second
        # asyncio.run would fail with "Event loop is closed".
        async def _post_and_transition() -> tuple[Any, str | None]:
            comment_id = await comment(external_id, body)
            if category is None:
                return comment_id, None
            transition = getattr(adapter, "transition_external", None)
            if transition is None:
                return comment_id, "unsupported"
            try:
                await transition(external_id, category)
            except Exception as exc:
                logger.exception("tracker status transition failed for %s (non-fatal)", external_id)
                return comment_id, f"failed: {exc}"[:200]
            return comment_id, "applied"

        comment_id, transition_record = asyncio.run(_post_and_transition())

        record: dict[str, Any] = {"status": "posted", "comment_id": str(comment_id)}
        if transition_record is not None:
            record["transition"] = transition_record
        _record_writeback(session_factory, run_id, record)
        return "posted"
    except Exception as exc:
        logger.exception("tracker write-back failed for run %s (non-fatal)", run_id)
        try:
            _record_writeback(
                session_factory, run_id, {"status": "failed", "error": str(exc)[:500]}
            )
        except Exception:
            logger.exception("tracker write-back outcome could not be recorded for %s", run_id)
        return "failed"


# --------------------------------------------------------------------------- #
# Production seams                                                            #
# --------------------------------------------------------------------------- #


def _session_factory() -> sessionmaker[Session]:  # pragma: no cover - prod seam
    from scf_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


def _connection_service(
    session_factory: sessionmaker[Session],
) -> PMConnectionService:  # pragma: no cover - prod seam
    from scf_api.auth.service import get_auth_service

    return PMConnectionService(
        session_factory=session_factory,
        vault=get_auth_service().vault,
        audit=AuditLog(),
    )


def dispatch_issue_task(
    delivery_row_id: str, payload: dict | None = None
) -> dict[str, str | None]:  # pragma: no cover - prod seam
    """Standalone re-drive: re-evaluate dispatch for one persisted delivery."""
    factory = _session_factory()
    connections = _connection_service(factory)
    with factory() as session:
        row = session.get(PMWebhookDelivery, uuid.UUID(delivery_row_id))
        if row is None or row.connection_id is None:
            return {"action": "skipped", "reason": "missing_delivery"}
        session.expunge(row)
    connection = connections.get_connection_any_workspace(row.connection_id)
    if connection is None:
        return {"action": "skipped", "reason": "missing_connection"}
    outcome = maybe_dispatch(
        factory,
        connection=connection,
        delivery=row,
        payload=payload,
        connections=connections,
        audit=AuditLog(),
    )
    return {"action": outcome.action, "reason": outcome.reason, "run_id": outcome.run_id}


def register_pm_dispatch_tasks() -> None:
    """Register the dispatch Celery task on the reliability base (idempotent)."""
    celery_app.task(name=PM_DISPATCH_TASK, base=ScfReliableTask)(dispatch_issue_task)


register_pm_dispatch_tasks()


__all__ = [
    "PM_DISPATCH_TASK",
    "DispatchOutcome",
    "dispatch_issue_task",
    "maybe_dispatch",
    "post_run_writeback",
    "register_pm_dispatch_tasks",
]
