"""PM webhook board-write task (F18): ``forge.pm.process_webhook``.

Completes the inbound half of the F18 sync loop that ``pm_service`` parked
before the F01 Postgres board substrate existed. The API side verifies,
dedupes, persists, and audits a webhook delivery, then enqueues this task with
the delivery row id; here the worker:

1. loads the persisted :class:`~forge_db.models.pm.PMWebhookDelivery` + its
   :class:`~forge_db.models.pm.PMConnection` (the row carries the tenant);
2. re-fetches the authoritative issue state through the provider adapter built
   by the registry seam (``forge_integrations.pm.registry.build_adapter`` via
   ``PMConnectionService`` — the payload is only ever a hint);
3. runs :meth:`~forge_integrations.pm.sync_engine.PMSyncEngine.sync_in`, which
   maps + upserts the board task through :class:`SqlBoardWriter` (the F01
   ``SqlAlchemyBoardService`` scoped to the connection's workspace) and links
   it durably via :class:`~forge_api.services.pm_link_repository_db.DbLinkRepository`.

Idempotency is layered: the intake already dedupes on ``delivery_id`` (a
provider redelivery never enqueues twice); a Celery redelivery of the same row
early-returns once the row is marked processed; and a *distinct* delivery with
identical content is echo-suppressed by the engine's content hash (no board
write, row marked ``echo_suppressed``).

Multi-tenancy: every write surface (board service, link repository, adapter
resolution) is bound to the connection's ``workspace_id`` — a delivery can
never write outside its own tenant.

Still parked (unchanged scope): OAuth code exchange, historical backfill,
manual conflict resolution, external delete propagation, and the outbound
``activity_events`` scan.

The pure function (``process_webhook``) is testable without Celery; the
``*_task`` body is the production seam (mirrors ``sprint_tasks.py``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from forge_api.observability.audit import AuditCategory, AuditLog
from forge_api.services.pm_link_repository_db import DbLinkRepository
from forge_api.services.pm_service import PMConnectionService
from forge_board import EntityNotFoundError, SqlAlchemyBoardService
from forge_contracts import TaskDTO
from forge_contracts.enums import Priority, TaskStatus
from forge_contracts.pm import (
    ConflictPolicy,
    ForgePriority,
    ForgeTask,
    StatusCategory,
    SyncDirection,
)
from forge_db.models.enums import PMConnectionStatus, PMDeliveryStatus, PMSyncDirection
from forge_db.models.pm import PMConnection, PMWebhookDelivery
from forge_integrations.pm.errors import ProviderError, RateLimitError
from forge_integrations.pm.sync_engine import ForgeTaskPatch, PMSyncEngine
from forge_worker.celery_app import celery_app
from forge_worker.reliability import ForgeTask as ForgeReliableTask
from forge_worker.reliability import TransientError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from forge_contracts.pm import ExternalTask, PMAdapter, SyncOutcome

logger = logging.getLogger(__name__)

#: Enqueued by name from ``forge_api.services.pm_service`` (the API cannot
#: import ``forge_worker``; the dependency runs the other way).
PM_SYNC_TASK = "forge.pm.process_webhook"

# StatusCategory <-> board TaskStatus: the sync grain is the *category*; the
# reverse map collapses the richer board states onto their category.
_CATEGORY_TO_STATUS: dict[StatusCategory, TaskStatus] = {
    StatusCategory.backlog: TaskStatus.BACKLOG,
    StatusCategory.unstarted: TaskStatus.READY,
    StatusCategory.started: TaskStatus.IN_PROGRESS,
    StatusCategory.completed: TaskStatus.DONE,
    StatusCategory.canceled: TaskStatus.CANCELLED,
}
_STATUS_TO_CATEGORY: dict[TaskStatus, StatusCategory] = {
    TaskStatus.BACKLOG: StatusCategory.backlog,
    TaskStatus.READY: StatusCategory.unstarted,
    TaskStatus.READY_FOR_AGENT: StatusCategory.unstarted,
    TaskStatus.IN_PROGRESS: StatusCategory.started,
    TaskStatus.IN_REVIEW: StatusCategory.started,
    TaskStatus.BLOCKED: StatusCategory.started,
    TaskStatus.DONE: StatusCategory.completed,
    TaskStatus.CANCELLED: StatusCategory.canceled,
}
# ForgePriority (PM grain, has ``none``) <-> board Priority (no ``none``).
_PRIORITY_TO_BOARD: dict[ForgePriority, Priority] = {
    ForgePriority.none: Priority.LOW,
    ForgePriority.low: Priority.LOW,
    ForgePriority.medium: Priority.MEDIUM,
    ForgePriority.high: Priority.HIGH,
    ForgePriority.urgent: Priority.URGENT,
}
_BOARD_TO_PRIORITY: dict[Priority, ForgePriority] = {
    Priority.LOW: ForgePriority.low,
    Priority.MEDIUM: ForgePriority.medium,
    Priority.HIGH: ForgePriority.high,
    Priority.URGENT: ForgePriority.urgent,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime:
    """Normalise a (possibly naive, e.g. SQLite-read) timestamp to aware UTC."""
    if value is None:
        return _now()
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _dto_to_forge_task(dto: TaskDTO) -> ForgeTask:
    """Project a board ``TaskDTO`` onto the PM-sync ``ForgeTask`` grain."""
    if dto.id is None or dto.project_id is None:  # pragma: no cover - persisted DTOs carry both
        raise ValueError("persisted TaskDTO must carry id + project_id")
    return ForgeTask(
        id=dto.id,
        key=dto.key or "",
        project_id=dto.project_id,
        title=dto.title,
        description_md=dto.description,
        status_category=_STATUS_TO_CATEGORY[dto.status],
        priority=_BOARD_TO_PRIORITY[dto.priority],
        assignee_email=None,  # board tasks key assignees by id, not email
        label_names=list(dto.labels),
        version=0,  # the board substrate carries no version column (see SqlBoardWriter)
        updated_at=_aware(dto.updated_at),
    )


class SqlBoardWriter:
    """The engine's ``BoardWriter`` over the F01 ``SqlAlchemyBoardService``.

    Bound to a single workspace, so every create/update lands in the
    connection's tenant only. ``expected_version`` is accepted but not enforced:
    the board substrate has no per-row version column yet; last-write-wins
    within the engine's own conflict detection (which compares content hashes,
    not versions, before ever calling ``update``).

    A patch's ``assignee_email`` is intentionally **not** written: the board keys
    assignees by ``assignee_id`` (a Forge user), not by an external email, so the
    inbound assignee is dropped at this seam rather than guessed at (resolving an
    external email to a Forge user is parked; see ``_dto_to_forge_task``).
    """

    def __init__(self, session_factory: sessionmaker[Session], workspace_id: uuid.UUID) -> None:
        self._board = SqlAlchemyBoardService(session_factory, workspace_id)

    def get(self, forge_task_id: uuid.UUID) -> ForgeTask | None:
        try:
            dto = self._board.get_task(forge_task_id)
        except EntityNotFoundError:
            return None
        return _dto_to_forge_task(dto)

    def create(self, patch: ForgeTaskPatch, *, source: dict) -> ForgeTask:
        dto = TaskDTO(
            project_id=patch.project_id,
            title=patch.title,
            description=patch.description_md,
            status=_CATEGORY_TO_STATUS[patch.status_category],
            priority=_PRIORITY_TO_BOARD[patch.priority],
            labels=list(patch.label_names),
        )
        return _dto_to_forge_task(self._board.create_task(dto))

    def update(
        self,
        forge_task_id: uuid.UUID,
        patch: ForgeTaskPatch,
        *,
        expected_version: int | None,
        source: dict,
    ) -> ForgeTask:
        current = self._board.get_task(forge_task_id)
        updated = current.model_copy(
            update={
                "title": patch.title,
                "description": patch.description_md,
                "status": _CATEGORY_TO_STATUS[patch.status_category],
                "priority": _PRIORITY_TO_BOARD[patch.priority],
                "labels": list(patch.label_names),
            }
        )
        return _dto_to_forge_task(self._board.update_task(forge_task_id, updated))


class _EngineAuditSink:
    """Adapt the engine's dict-record ``AuditSink`` onto the structured AuditLog."""

    def __init__(self, audit: AuditLog, workspace_id: uuid.UUID) -> None:
        self._audit = audit
        self._workspace_id = workspace_id

    def record(self, entry: dict) -> None:
        self._audit.record(
            category=AuditCategory.SYSTEM,
            action=f"pm_sync_{entry.get('operation', 'op')}",
            workspace_id=self._workspace_id,
            connection_id=entry.get("connection_id"),
            target=entry.get("external_id"),
            status=str(entry.get("result", "ok")),
            payload_hash=entry.get("payload_hash"),
            metadata={
                "provider": entry.get("provider"),
                "direction": entry.get("direction"),
                "forge_task_id": entry.get("forge_task_id"),
            },
        )


def _mark(
    session_factory: sessionmaker[Session],
    row_id: uuid.UUID,
    status: PMDeliveryStatus,
    *,
    error: str | None = None,
) -> None:
    with session_factory() as session:
        row = session.get(PMWebhookDelivery, row_id)
        if row is None:  # pragma: no cover - row deleted mid-flight
            return
        row.status = status
        row.processed_at = _now()
        row.error = error
        session.commit()


def _result(action: str, row_id: uuid.UUID, **extra: str | None) -> dict[str, str | None]:
    return {"action": action, "delivery_row_id": str(row_id), **extra}


def _is_transient(exc: BaseException) -> bool:
    """Is ``exc`` a *retryable* provider failure (vs. a deterministic one)?

    Transient (retry with backoff): a provider rate-limit (429 — always resolves)
    or an upstream **5xx** ``ProviderError`` blip, plus raw network faults
    (connect / timeout). Everything else is deterministic and must fail fast to
    ``ERROR`` — auth, not-found, unmappable field, sync-conflict, an *unsupported*
    provider (a ``ProviderError`` with no 5xx status, e.g. the registry's
    ``unsupported PM provider``), or a malformed payload — because retrying it
    only burns the budget and delays the terminal state.
    """
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, ProviderError):
        return exc.status_code is not None and exc.status_code >= 500
    return isinstance(exc, ConnectionError | TimeoutError)


async def _fetch_and_sync(
    adapter: PMAdapter, engine: PMSyncEngine, external_id: str
) -> SyncOutcome:
    external: ExternalTask = await adapter.fetch_external(external_id)
    return await engine.sync_in(external)


def process_webhook(
    session_factory: sessionmaker[Session],
    delivery_row_id: uuid.UUID,
    *,
    connections: PMConnectionService,
    audit: AuditLog | None = None,
) -> dict[str, str | None]:
    """Re-fetch the changed issue for one persisted delivery and upsert the board.

    Idempotent: an already-processed row early-returns; identical redelivered
    content is echo-suppressed by the engine's content hash.

    Failure semantics (honest): any fetch / board-write failure first marks the
    row ``error`` (truncated). A *transient* provider failure — a rate-limit, an
    upstream 5xx, a raw network blip — is then re-raised as
    :class:`~forge_worker.reliability.TransientError`, so the
    :class:`~forge_worker.reliability.ForgeTask` base auto-retries it ``N`` times
    with exponential backoff; only once that budget is spent does the row stay
    ``ERROR``. A *permanent* failure — auth, not-found, unmappable field, an
    unsupported provider, a malformed payload — is re-raised as-is and lands
    ``ERROR`` immediately (retrying it only burns the budget). Either way the
    independent recovery path is the next webhook for the same issue, which
    re-fetches authoritative state and re-runs from ``received``.
    """
    audit = audit or AuditLog()
    with session_factory() as session:
        row = session.get(PMWebhookDelivery, delivery_row_id)
        if row is None:
            logger.warning("pm_sync: delivery row %s not found; dropping", delivery_row_id)
            return _result("missing_delivery", delivery_row_id)
        session.expunge(row)

    if row.status in (PMDeliveryStatus.PROCESSED, PMDeliveryStatus.ECHO_SUPPRESSED):
        return _result("already_processed", delivery_row_id)
    if row.status == PMDeliveryStatus.SKIPPED:
        return _result("skipped", delivery_row_id)

    if row.event_type == "issue.deleted":
        # External delete propagation follows the connection's
        # ``on_external_delete`` policy — parked; never a blind board delete.
        _mark(session_factory, delivery_row_id, PMDeliveryStatus.SKIPPED)
        return _result("skipped_delete", delivery_row_id)

    if row.connection_id is None or row.external_id is None:
        _mark(session_factory, delivery_row_id, PMDeliveryStatus.SKIPPED)
        return _result("skipped", delivery_row_id)

    connection = connections.get_connection_any_workspace(row.connection_id)
    if connection is None:
        _mark(session_factory, delivery_row_id, PMDeliveryStatus.SKIPPED)
        return _result("skipped", delivery_row_id)
    if (
        connection.status == PMConnectionStatus.DISABLED
        or connection.sync_direction == PMSyncDirection.OUTBOUND_ONLY
    ):
        _mark(session_factory, delivery_row_id, PMDeliveryStatus.SKIPPED)
        return _result("skipped", delivery_row_id)

    try:
        outcome = _sync_delivery(session_factory, connections, connection, row, audit)
    except Exception as exc:
        _mark(session_factory, delivery_row_id, PMDeliveryStatus.ERROR, error=str(exc)[:2000])
        logger.exception(
            "pm_sync: board write failed for delivery %s (connection %s)",
            delivery_row_id,
            row.connection_id,
        )
        # Transient provider blips retry with backoff on the ForgeTask base;
        # deterministic failures fail fast to ERROR (see module docstring).
        if _is_transient(exc):
            raise TransientError(str(exc)) from exc
        raise

    status = (
        PMDeliveryStatus.ECHO_SUPPRESSED
        if outcome.action == "skipped_echo"
        else PMDeliveryStatus.PROCESSED
    )
    _mark(session_factory, delivery_row_id, status)
    return _result(
        outcome.action,
        delivery_row_id,
        forge_task_id=str(outcome.forge_task_id) if outcome.forge_task_id else None,
        external_id=outcome.external_id,
    )


def _sync_delivery(
    session_factory: sessionmaker[Session],
    connections: PMConnectionService,
    connection: PMConnection,
    row: PMWebhookDelivery,
    audit: AuditLog,
) -> SyncOutcome:
    # Resolved through the registry seam (``build_adapter``) and scoped to the
    # connection's workspace + project — never a cross-tenant adapter.
    adapter = connections.get_adapter_for_project(
        connection.workspace_id, connection.project_id, connection.provider.value
    )
    engine = PMSyncEngine(
        adapter=adapter,
        links=DbLinkRepository(session_factory),
        board=SqlBoardWriter(session_factory, connection.workspace_id),
        audit=_EngineAuditSink(audit, connection.workspace_id),
        connection_id=connection.id,
        workspace_id=connection.workspace_id,
        forge_project_id=connection.project_id,
        conflict_policy=ConflictPolicy(connection.conflict_policy.value),
        sync_direction=SyncDirection(connection.sync_direction.value),
    )
    assert row.external_id is not None  # guarded by the caller
    return asyncio.run(_fetch_and_sync(adapter, engine, row.external_id))


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


def process_webhook_task(delivery_row_id: str) -> dict[str, str | None]:  # pragma: no cover
    factory = _session_factory()
    return process_webhook(
        factory,
        uuid.UUID(delivery_row_id),
        connections=_connection_service(factory),
        audit=AuditLog(),
    )


def register_pm_sync_tasks() -> None:
    """Register the PM sync Celery task on the reliability base (idempotent).

    ``ForgeTask`` gives the task ``acks_late`` + auto-retry of
    :class:`~forge_worker.reliability.TransientError` with exponential backoff
    (mirrors ``agent_runner`` / ``syncer`` / ``indexer``), so a transient
    provider blip re-fetches instead of dropping the delivery — recovery no
    longer relies solely on the next webhook event.
    """
    celery_app.task(name=PM_SYNC_TASK, base=ForgeReliableTask)(process_webhook_task)


register_pm_sync_tasks()


__all__ = [
    "PM_SYNC_TASK",
    "SqlBoardWriter",
    "process_webhook",
    "process_webhook_task",
    "register_pm_sync_tasks",
]
