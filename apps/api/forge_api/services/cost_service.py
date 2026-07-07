"""Cost service (F38 §3.2): workspace isolation + RBAC glue over the ledger.

Every read is scoped by the authenticated principal's ``workspace_id``; a
``scope_id`` belonging to another workspace (or to nothing) surfaces as
:class:`ScopeNotFoundError` -> HTTP 404 — no existence leak (spec AC14).

Price-book mutation + reprice are admin-only at the router and emit immutable
``cost.price_set`` / ``cost.repriced`` events through F39's ``AuditSink``
contract (the shared append-only ``audit_log`` table in the Sql wiring).

Seams (in-memory implementations back the hermetic API tests; the Sql ones are
the production wiring — same pattern as the marketplace service):

- ``CostReader`` / ``CostLedger`` / ``PriceBook`` come from ``forge_obs.cost``.
- :class:`ScopeResolver` maps a project/task scope id to its workspace.
- :class:`PriceStore` lists/creates ``model_price`` rows.

Conformance note: the slice doc enqueues reprice onto a Celery queue; the
foundation precedent (marketplace, deployments) executes service logic
synchronously and exposes the same core through the worker task — the
``cost.reprice`` Celery task calls this same repository path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from forge_api.schemas.cost import (
    CostSummary,
    CostTimeseries,
    ModelPrice,
    PriceCreateRequest,
)
from forge_contracts.audit import AuditEvent, AuditSink
from forge_db.models import Project, Task
from forge_db.models.cost import ModelPrice as ModelPriceRow
from forge_db.models.enums import CostEventKind
from forge_obs.cost.models import COST_KINDS
from forge_obs.cost.pricing import PriceBook
from forge_obs.cost.repository import CostLedger, CostReader

__all__ = [
    "CostService",
    "InMemoryPriceStore",
    "InMemoryScopeResolver",
    "PriceStore",
    "ScopeNotFoundError",
    "ScopeResolver",
    "SessionAuditSink",
    "SqlPriceStore",
    "SqlScopeResolver",
]


class ScopeNotFoundError(LookupError):
    """Unknown scope id OR a cross-workspace scope id (both -> 404)."""


class ScopeResolver(Protocol):
    """Maps a (scope, scope_id) to the owning workspace id, or ``None``."""

    def workspace_of(self, scope: str, scope_id: uuid.UUID) -> uuid.UUID | None: ...


class PriceStore(Protocol):
    """List/create ``model_price`` rows (reads include the global defaults)."""

    def list_prices(
        self,
        *,
        workspace_id: uuid.UUID,
        provider: str | None,
        model: str | None,
    ) -> list[ModelPrice]: ...

    def create_price(
        self,
        *,
        workspace_id: uuid.UUID,
        data: PriceCreateRequest,
        created_by: uuid.UUID | None,
    ) -> ModelPrice: ...


# --------------------------------------------------------------------------- #
# Sql seam implementations (production wiring)                                 #
# --------------------------------------------------------------------------- #


class SqlScopeResolver:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def workspace_of(self, scope: str, scope_id: uuid.UUID) -> uuid.UUID | None:
        model = {"project": Project, "task": Task}.get(scope)
        if model is None:
            return None
        with self._session_factory() as session:
            row = session.get(model, scope_id)
            return None if row is None else row.workspace_id


def _price_dto(row: ModelPriceRow) -> ModelPrice:
    return ModelPrice(
        id=row.id,
        workspace_id=row.workspace_id,
        provider=row.provider,
        model=row.model,
        kind=row.kind.value if hasattr(row.kind, "value") else row.kind,
        prompt_usd_per_1k=row.prompt_usd_per_1k,
        completion_usd_per_1k=row.completion_usd_per_1k,
        currency=row.currency,
        effective_from=row.effective_from,
    )


class SqlPriceStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_prices(
        self,
        *,
        workspace_id: uuid.UUID,
        provider: str | None,
        model: str | None,
    ) -> list[ModelPrice]:
        with self._session_factory() as session:
            stmt = select(ModelPriceRow).where(
                or_(
                    ModelPriceRow.workspace_id == workspace_id,
                    ModelPriceRow.workspace_id.is_(None),
                )
            )
            if provider is not None:
                stmt = stmt.where(ModelPriceRow.provider == provider)
            if model is not None:
                stmt = stmt.where(ModelPriceRow.model == model)
            stmt = stmt.order_by(
                ModelPriceRow.provider, ModelPriceRow.model, ModelPriceRow.effective_from.desc()
            )
            return [_price_dto(row) for row in session.scalars(stmt)]

    def create_price(
        self,
        *,
        workspace_id: uuid.UUID,
        data: PriceCreateRequest,
        created_by: uuid.UUID | None,
    ) -> ModelPrice:
        row = ModelPriceRow(
            workspace_id=workspace_id,
            provider=data.provider,
            model=data.model,
            kind=CostEventKind(data.kind),
            prompt_usd_per_1k=data.prompt_usd_per_1k,
            completion_usd_per_1k=data.completion_usd_per_1k,
            currency=data.currency,
            effective_from=data.effective_from or datetime.now(UTC),
            created_by=created_by,
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            return _price_dto(row)


class SessionAuditSink:
    """AuditSink over the shared append-only ``audit_log`` (one txn per event)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def emit(self, event: AuditEvent) -> None:
        from forge_api.services.audit import SqlAuditWriter

        with self._session_factory() as session:
            SqlAuditWriter(session).emit(event)
            session.commit()


# --------------------------------------------------------------------------- #
# In-memory seam implementations (hermetic tests / no-DB contexts)             #
# --------------------------------------------------------------------------- #


class InMemoryScopeResolver:
    def __init__(self, owners: dict[uuid.UUID, uuid.UUID] | None = None) -> None:
        self.owners = owners or {}

    def workspace_of(self, scope: str, scope_id: uuid.UUID) -> uuid.UUID | None:
        del scope
        return self.owners.get(scope_id)


class InMemoryPriceStore:
    def __init__(self) -> None:
        self.prices: list[ModelPrice] = []

    def list_prices(
        self,
        *,
        workspace_id: uuid.UUID,
        provider: str | None,
        model: str | None,
    ) -> list[ModelPrice]:
        return [
            p
            for p in self.prices
            if p.workspace_id in (None, workspace_id)
            and (provider is None or p.provider == provider)
            and (model is None or p.model == model)
        ]

    def create_price(
        self,
        *,
        workspace_id: uuid.UUID,
        data: PriceCreateRequest,
        created_by: uuid.UUID | None,
    ) -> ModelPrice:
        del created_by
        price = ModelPrice(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            provider=data.provider,
            model=data.model,
            kind=data.kind,
            prompt_usd_per_1k=data.prompt_usd_per_1k,
            completion_usd_per_1k=data.completion_usd_per_1k,
            currency=data.currency,
            effective_from=data.effective_from or datetime.now(UTC),
        )
        self.prices.append(price)
        return price


# --------------------------------------------------------------------------- #
# The service                                                                  #
# --------------------------------------------------------------------------- #


class CostService:
    """Workspace-isolated cost reads + admin price-book/reprice mutations."""

    def __init__(
        self,
        *,
        reader: CostReader,
        ledger: CostLedger,
        price_book: PriceBook,
        prices: PriceStore,
        scopes: ScopeResolver,
        audit: AuditSink,
    ) -> None:
        self._reader = reader
        self._ledger = ledger
        self._price_book = price_book
        self._prices = prices
        self._scopes = scopes
        self._audit = audit

    # -- isolation ------------------------------------------------------------ #

    def _check_scope(self, workspace_id: uuid.UUID, scope: str, scope_id: uuid.UUID) -> None:
        if scope == "workspace":
            if scope_id != workspace_id:
                raise ScopeNotFoundError(scope_id)
            return
        if scope in ("project", "task"):
            owner = self._scopes.workspace_of(scope, scope_id)
            if owner is None or owner != workspace_id:
                raise ScopeNotFoundError(scope_id)
            return
        raise ValueError(f"unknown scope {scope!r}")

    # -- reads (viewer+) -------------------------------------------------------- #

    def task_cost(self, *, workspace_id: uuid.UUID, task_id: uuid.UUID) -> CostSummary:
        self._check_scope(workspace_id, "task", task_id)
        return self._reader.summary(
            workspace_id=workspace_id,
            scope="task",
            scope_id=task_id,
            group_by="phase",
            frm=None,
            to=None,
        )

    def summary(
        self,
        *,
        workspace_id: uuid.UUID,
        scope: str,
        scope_id: uuid.UUID,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostSummary:
        self._check_scope(workspace_id, scope, scope_id)
        return self._reader.summary(
            workspace_id=workspace_id,
            scope=scope,
            scope_id=scope_id,
            group_by=group_by,
            frm=frm,
            to=to,
        )

    def timeseries(
        self,
        *,
        workspace_id: uuid.UUID,
        scope: str,
        scope_id: uuid.UUID,
        bucket: str,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostTimeseries:
        self._check_scope(workspace_id, scope, scope_id)
        return self._reader.timeseries(
            workspace_id=workspace_id,
            scope=scope,
            scope_id=scope_id,
            bucket=bucket,
            group_by=group_by,
            frm=frm,
            to=to,
        )

    def list_prices(
        self,
        *,
        workspace_id: uuid.UUID,
        provider: str | None = None,
        model: str | None = None,
    ) -> list[ModelPrice]:
        return self._prices.list_prices(workspace_id=workspace_id, provider=provider, model=model)

    # -- admin mutations (audited) ---------------------------------------------- #

    def set_price(
        self,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        data: PriceCreateRequest,
    ) -> ModelPrice:
        if data.kind not in COST_KINDS:
            raise ValueError(f"unknown price kind {data.kind!r}")
        price = self._prices.create_price(workspace_id=workspace_id, data=data, created_by=actor_id)
        self._audit.emit(
            AuditEvent(
                workspace_id=workspace_id,
                action="cost.price_set",
                actor_id=actor_id,
                target_type="model_price",
                target_id=price.id,
                after={
                    "provider": price.provider,
                    "model": price.model,
                    "kind": price.kind,
                    "prompt_usd_per_1k": str(price.prompt_usd_per_1k),
                    "completion_usd_per_1k": str(price.completion_usd_per_1k),
                    "effective_from": price.effective_from.isoformat(),
                },
            )
        )
        return price

    def reprice(
        self,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        since: datetime,
        provider: str | None = None,
        model: str | None = None,
    ) -> int:
        updated = self._ledger.reprice(
            workspace_id=workspace_id,
            since=since,
            provider=provider,
            model=model,
            price_book=self._price_book,
        )
        self._audit.emit(
            AuditEvent(
                workspace_id=workspace_id,
                action="cost.repriced",
                actor_id=actor_id,
                target_type="cost_event",
                details={
                    "since": since.isoformat(),
                    "provider": provider,
                    "model": model,
                    "updated": updated,
                },
            )
        )
        return updated
