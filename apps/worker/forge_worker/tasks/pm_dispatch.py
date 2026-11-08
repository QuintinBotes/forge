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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import or_, select

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
from forge_contracts import AgentObjective, AgentRunResult, RunStatus
from forge_db.models import AgentRun
from forge_db.models.enums import PMConnectionStatus, PMProvider, PMSyncDirection
from forge_db.models.enums import RunStatus as DbRunStatus
from forge_db.models.pm import PMConnection, PMTaskLink, PMWebhookDelivery
from forge_integrations.github import GitHubClient
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

    A PENDING/RUNNING row counts as active only when created within the
    hard task time limit plus 10 minutes, so a run whose worker died without
    recording an outcome cannot block its issue and cap slot forever.
    """
    now = _aware(now) if now is not None else datetime.now(UTC)
    recent_cutoff = now - timedelta(days=7)
    # shortcut: this age bound stands in for a stale-run reaper that should
    # terminally close dead PENDING/RUNNING runs; replace the bound with it.
    active_cutoff = now - timedelta(seconds=_hard_time_limit_s() + 600)
    tracker_connection = AgentRun.inputs[("context", "tracker", "connection_id")].as_string()
    tracker_external = AgentRun.inputs[("context", "tracker", "external_id")].as_string()
    tracker_delivery = AgentRun.inputs[("context", "tracker", "delivery_id")].as_string()
    tracker_hash = AgentRun.inputs[("context", "tracker", "payload_hash")].as_string()
    rows = session.execute(
        select(
            AgentRun.status,
            AgentRun.created_at,
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
    for status, created_at, run_external, run_delivery, run_hash in rows:
        if run_delivery == delivery_id or (payload_hash and run_hash == payload_hash):
            return "already_dispatched"
        if status in _ACTIVE_RUN_STATUSES and _aware(created_at) > active_cutoff:
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
        row = session.get(AgentRun, run_id)
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
        lines = [f"Forge run `{run_id}` finished with state **{result.status.value}**."]
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
