"""Issue-driven dispatch + run write-back (spec M M3/M2).

Two halves, both driven from data forge already persists:

* :func:`maybe_dispatch` — given one verified, deduped GitHub Issues webhook
  delivery plus the adapter's parsed hint subset (label / sender / repository),
  decide *fail-closed* (ADR-0007) whether a label event may start an agent
  run, and if so create + enqueue it through the same code path as
  ``POST /agent/runs`` (:func:`forge_api.services.agent_dispatch.create_and_enqueue_run`).
  The objective is built from the issue *re-fetched through the adapter*,
  never from the webhook payload (ADR-0014). Every refusal is returned as a
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
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.schemas.pm import PMDispatchConfig
from forge_api.services.agent_dispatch import (
    AgentEnqueueFailed,
    RepoNotConnected,
    build_tracker_objective,
    create_and_enqueue_run,
    repo_connected,
)
from forge_api.services.pm_service import PMConnectionService
from forge_contracts import AgentObjective, AgentRunResult
from forge_contracts.pm import StatusCategory
from forge_db.models import AgentRun
from forge_db.models.enums import PMProvider
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_db.models.pm import PMConnection, PMTaskLink, PMWebhookDelivery
from forge_integrations.pm.sync_engine import run_comment_marker
from forge_worker.celery_app import celery_app
from forge_worker.reliability import ForgeTask as ForgeReliableTask

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

#: Standalone re-drive task for one delivery's dispatch decision. Registered
#: when this module is imported — which ``forge_worker.tasks.pm_sync`` (in the
#: Celery ``include`` list) does — mirroring ``register_pm_sync_tasks``.
PM_DISPATCH_TASK = "forge.pm.dispatch_issue"

_ACTIVE_RUN_STATUSES = (DbRunStatus.PENDING, DbRunStatus.RUNNING)


@dataclass(frozen=True)
class DispatchOutcome:
    """One dispatch evaluation: ``not_applicable`` | ``skipped`` | ``dispatched``."""

    action: str
    reason: str | None = None
    run_id: str | None = None


def _default_run_enqueue(objective: AgentObjective) -> None:  # pragma: no cover - prod seam
    from forge_api.routers.agent import enqueue_agent_run

    enqueue_agent_run(objective)


def _parse_dispatch_config(connection: PMConnection) -> PMDispatchConfig | None:
    try:
        return PMDispatchConfig.model_validate((connection.config or {}).get("dispatch") or {})
    except ValidationError:
        return None


def _tracker_block(run: AgentRun) -> dict:
    context = (run.inputs or {}).get("context") or {}
    tracker = context.get("tracker") or {}
    return tracker if isinstance(tracker, dict) else {}


def _dispatch_guard(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    connection_id: uuid.UUID,
    external_id: str,
    delivery_id: str,
    max_active_runs: int,
) -> str | None:
    """A skip reason when an idempotency / concurrency guard trips, else None.

    Guards, in order: this very delivery already produced a run (a re-queued
    task can never enqueue twice); another run for the same issue is still
    queued/running; the connection is at its ``max_active_runs`` cap.
    """
    runs = (
        session.execute(select(AgentRun).where(AgentRun.workspace_id == workspace_id))
        .scalars()
        .all()
    )
    active_for_connection = 0
    for run in runs:
        tracker = _tracker_block(run)
        if tracker.get("connection_id") != str(connection_id):
            continue
        if tracker.get("delivery_id") == delivery_id:
            return "already_dispatched"
        if run.status in _ACTIVE_RUN_STATUSES:
            if tracker.get("external_id") == external_id:
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
        or payload_repo.lower() != connection.external_project_key.lower()
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
        task_id=task_id,
    )
    with session_factory() as session:
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
        except AgentEnqueueFailed:
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

    Returns ``"posted"``, ``"already_recorded"``, ``"unsupported"`` or
    ``"failed"``. The outcome is stamped onto the run row's output under
    ``tracker_writeback`` (the exactly-once guard for redeliveries) without
    touching the run's recorded status. ``status_on_pr_opened`` is applied
    only when the run actually opened a pull request.
    """
    try:
        with session_factory() as session:
            row = session.get(AgentRun, run_id)
            if row is not None and (row.output or {}).get("tracker_writeback"):
                return "already_recorded"
        connection_id = uuid.UUID(str(tracker["connection_id"]))
        external_id = str(tracker["external_id"])
        connection = connections.get_connection_any_workspace(connection_id)
        if connection is None or connection.workspace_id != workspace_id:
            raise LookupError(f"tracker connection {connection_id} not in run workspace")
        adapter: Any = connections.get_adapter_for_project(
            connection.workspace_id, connection.project_id, connection.provider.value
        )
        comment = getattr(adapter, "comment_external", None)
        if comment is None:
            _record_writeback(session_factory, run_id, {"status": "unsupported"})
            return "unsupported"

        pr = (result.artifacts or {}).get("pull_request") or {}
        pr_opened = pr.get("status") == "opened" and bool(pr.get("url"))
        lines = [f"Forge run `{run_id}` finished with state **{result.status.value}**."]
        if pr_opened:
            lines.append(f"Pull request: {pr['url']}")
        lines.append(run_comment_marker(run_id))
        comment_id = asyncio.run(comment(external_id, "\n\n".join(lines)))

        record: dict[str, Any] = {"status": "posted", "comment_id": str(comment_id)}
        if pr_opened:
            config = _parse_dispatch_config(connection)
            if config is not None and config.status_on_pr_opened is not None:
                record["transition"] = _apply_status_transition(
                    adapter, external_id, config.status_on_pr_opened
                )
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


def _apply_status_transition(adapter: Any, external_id: str, category: StatusCategory) -> str:
    transition = getattr(adapter, "transition_external", None)
    if transition is None:
        return "unsupported"
    try:
        asyncio.run(transition(external_id, category))
    except Exception as exc:
        logger.exception("tracker status transition failed for %s (non-fatal)", external_id)
        return f"failed: {exc}"[:200]
    return "applied"


# --------------------------------------------------------------------------- #
# Production seams                                                            #
# --------------------------------------------------------------------------- #


def _session_factory() -> sessionmaker[Session]:  # pragma: no cover - prod seam
    from forge_db import create_db_engine, create_session_factory, get_database_url

    return create_session_factory(create_db_engine(get_database_url()))


def _connection_service(
    session_factory: sessionmaker[Session],
) -> PMConnectionService:  # pragma: no cover - prod seam
    from forge_api.auth.service import get_auth_service

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
    celery_app.task(name=PM_DISPATCH_TASK, base=ForgeReliableTask)(dispatch_issue_task)


register_pm_dispatch_tasks()


__all__ = [
    "PM_DISPATCH_TASK",
    "DispatchOutcome",
    "dispatch_issue_task",
    "maybe_dispatch",
    "post_run_writeback",
    "register_pm_dispatch_tasks",
]
