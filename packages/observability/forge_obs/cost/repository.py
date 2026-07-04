"""Cost ledger repositories: the durable system of record for token cost.

Two implementations of the same seams:

- :class:`InMemoryCostLedger` — hermetic (unit tests, API handler tests, and
  the no-DB degraded path), mirroring the earlier-slice precedent of in-memory
  service state.
- :class:`SqlCostLedger` / :class:`SqlCostReader` — the real ``cost_event`` /
  ``model_price`` tables via ``forge_db`` (sync sessions, matching the
  foundation; the slice doc's async signatures are conformed to sync).

Append-only discipline (spec §3.1): rows are inserted once, idempotent on
``(workspace_id, request_id)``; the ONLY mutation path is the audited
``reprice`` which recomputes ``cost_usd``/``price_id`` from the price book.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from forge_obs.cost.models import (
    CostBucket,
    CostRecord,
    CostSummary,
    CostTimeseries,
    ModelUsage,
)
from forge_obs.cost.pricing import PriceBook, compute_cost

__all__ = [
    "CostLedger",
    "CostReader",
    "InMemoryCostLedger",
    "SqlCostLedger",
    "SqlCostReader",
]

_GROUPS = frozenset({"phase", "provider", "model", "none"})
_BUCKETS = frozenset({"hour", "day", "week"})
_SCOPES = frozenset({"workspace", "project", "task"})


@runtime_checkable
class CostLedger(Protocol):
    """Write side: idempotent upsert + the audited reprice path."""

    def upsert_event(
        self, usage: ModelUsage, *, cost: Decimal, price_id: UUID | None
    ) -> CostRecord: ...

    def reprice(
        self,
        *,
        workspace_id: UUID,
        since: datetime,
        provider: str | None,
        model: str | None,
        price_book: PriceBook,
    ) -> int: ...


@runtime_checkable
class CostReader(Protocol):
    """Read side: workspace-scoped rollups for the Cost API."""

    def summary(
        self,
        *,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostSummary: ...

    def timeseries(
        self,
        *,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        bucket: str,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostTimeseries: ...


def _validate(scope: str, group_by: str, bucket: str = "day") -> None:
    if scope not in _SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if group_by not in _GROUPS:
        raise ValueError(f"unknown group_by {group_by!r}")
    if bucket not in _BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}")


def _truncate(ts: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "week":  # ISO week, Monday start
        return day - timedelta(days=day.weekday())
    return day


def _group_key(group_by: str, *, phase: str | None, provider: str, model: str) -> str:
    if group_by == "phase":
        return phase or "unknown"
    if group_by == "provider":
        return provider
    if group_by == "model":
        return model
    return "total"


# --------------------------------------------------------------------------- #
# In-memory implementation                                                     #
# --------------------------------------------------------------------------- #


class InMemoryCostLedger:
    """Hermetic ledger + reader over plain dict rows (keyed for idempotency)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str], dict[str, Any]] = {}

    # -- write side ---------------------------------------------------------- #

    def upsert_event(
        self, usage: ModelUsage, *, cost: Decimal, price_id: UUID | None
    ) -> CostRecord:
        key = (usage.workspace_id, usage.request_id)
        existing = self._rows.get(key)
        if existing is not None:
            return CostRecord(
                cost_event_id=existing["id"],
                cost_usd=existing["cost_usd"],
                priced=existing["price_id"] is not None,
                price_id=existing["price_id"],
                deduplicated=True,
            )
        row = {
            "id": uuid4(),
            **usage.model_dump(),
            "cost_usd": cost,
            "price_id": price_id,
        }
        self._rows[key] = row
        return CostRecord(
            cost_event_id=row["id"], cost_usd=cost, priced=price_id is not None, price_id=price_id
        )

    def reprice(
        self,
        *,
        workspace_id: UUID,
        since: datetime,
        provider: str | None,
        model: str | None,
        price_book: PriceBook,
    ) -> int:
        updated = 0
        for row in self._rows.values():
            if row["workspace_id"] != workspace_id or row["occurred_at"] < since:
                continue
            if provider is not None and row["provider"] != provider:
                continue
            if model is not None and row["model"] != model:
                continue
            usage = ModelUsage(**{k: row[k] for k in ModelUsage.model_fields})
            price = price_book.resolve(
                workspace_id=workspace_id,
                provider=row["provider"],
                model=row["model"],
                kind=row["kind"],
                at=row["occurred_at"],
            )
            new_cost = compute_cost(usage, price)
            new_price_id = price.id if price is not None else None
            if new_cost != row["cost_usd"] or new_price_id != row["price_id"]:
                row["cost_usd"] = new_cost
                row["price_id"] = new_price_id
                updated += 1
        return updated

    # -- read side ------------------------------------------------------------ #

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows.values())

    def _scoped(
        self,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        frm: datetime | None,
        to: datetime | None,
    ) -> list[dict[str, Any]]:
        field = {"workspace": "workspace_id", "project": "project_id", "task": "task_id"}[scope]
        out = []
        for row in self._rows.values():
            if row["workspace_id"] != workspace_id or row[field] != scope_id:
                continue
            if frm is not None and row["occurred_at"] < frm:
                continue
            if to is not None and row["occurred_at"] >= to:
                continue
            out.append(row)
        return out

    def summary(
        self,
        *,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostSummary:
        _validate(scope, group_by)
        buckets: dict[str, CostBucket] = {}
        total = Decimal(0)
        prompt = completion = 0
        for row in self._scoped(workspace_id, scope, scope_id, frm, to):
            key = _group_key(
                group_by, phase=row["phase"], provider=row["provider"], model=row["model"]
            )
            entry = buckets.setdefault(key, CostBucket(key=key, cost_usd=Decimal(0)))
            entry.cost_usd += row["cost_usd"]
            entry.prompt_tokens += row["prompt_tokens"]
            entry.completion_tokens += row["completion_tokens"]
            total += row["cost_usd"]
            prompt += row["prompt_tokens"]
            completion += row["completion_tokens"]
        return CostSummary(
            scope=scope,
            scope_id=scope_id,
            total_cost_usd=total,
            total_prompt_tokens=prompt,
            total_completion_tokens=completion,
            group_by=group_by,
            buckets=sorted(buckets.values(), key=lambda b: b.key),
            from_=frm,
            to=to,
        )

    def timeseries(
        self,
        *,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        bucket: str,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostTimeseries:
        _validate(scope, group_by, bucket)
        series: dict[str, dict[datetime, Decimal]] = {}
        for row in self._scoped(workspace_id, scope, scope_id, frm, to):
            key = _group_key(
                group_by, phase=row["phase"], provider=row["provider"], model=row["model"]
            )
            ts = _truncate(row["occurred_at"], bucket)
            per_key = series.setdefault(key, {})
            per_key[ts] = per_key.get(ts, Decimal(0)) + row["cost_usd"]
        return CostTimeseries(
            scope=scope,
            scope_id=scope_id,
            bucket=bucket,
            group_by=group_by,
            series={k: sorted(v.items()) for k, v in sorted(series.items())},
        )


# --------------------------------------------------------------------------- #
# SQL implementation (forge_db)                                                #
# --------------------------------------------------------------------------- #


def _usage_from_row(row: Any) -> ModelUsage:
    return ModelUsage(
        workspace_id=row.workspace_id,
        request_id=row.request_id,
        provider=row.provider,
        model=row.model,
        kind=row.kind.value if hasattr(row.kind, "value") else row.kind,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        occurred_at=row.occurred_at,
        project_id=row.project_id,
        task_id=row.task_id,
        workflow_run_id=row.workflow_run_id,
        agent_run_id=row.agent_run_id,
        step_id=row.step_id,
        phase=row.phase,
    )


class SqlCostLedger:
    """Durable ledger over ``cost_event`` (idempotent on the unique index)."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def upsert_event(
        self, usage: ModelUsage, *, cost: Decimal, price_id: UUID | None
    ) -> CostRecord:
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from forge_db.models.cost import CostEvent, CostEventKind

        def _dedup(session) -> CostRecord | None:
            row = session.scalars(
                select(CostEvent).where(
                    CostEvent.workspace_id == usage.workspace_id,
                    CostEvent.request_id == usage.request_id,
                )
            ).first()
            if row is None:
                return None
            return CostRecord(
                cost_event_id=row.id,
                cost_usd=row.cost_usd,
                priced=row.price_id is not None,
                price_id=row.price_id,
                deduplicated=True,
            )

        with self._session_factory() as session:
            existing = _dedup(session)
            if existing is not None:
                return existing
            row = CostEvent(
                workspace_id=usage.workspace_id,
                project_id=usage.project_id,
                task_id=usage.task_id,
                workflow_run_id=usage.workflow_run_id,
                agent_run_id=usage.agent_run_id,
                step_id=usage.step_id,
                phase=usage.phase,
                kind=CostEventKind(usage.kind),
                provider=usage.provider,
                model=usage.model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=cost,
                price_id=price_id,
                request_id=usage.request_id,
                occurred_at=usage.occurred_at,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Lost a concurrent race on (workspace_id, request_id): dedup.
                session.rollback()
                raced = _dedup(session)
                if raced is not None:
                    return raced
                raise
            return CostRecord(
                cost_event_id=row.id,
                cost_usd=cost,
                priced=price_id is not None,
                price_id=price_id,
            )

    def reprice(
        self,
        *,
        workspace_id: UUID,
        since: datetime,
        provider: str | None,
        model: str | None,
        price_book: PriceBook,
    ) -> int:
        from sqlalchemy import select

        from forge_db.models.cost import CostEvent

        updated = 0
        with self._session_factory() as session:
            stmt = select(CostEvent).where(
                CostEvent.workspace_id == workspace_id,
                CostEvent.occurred_at >= since,
            )
            if provider is not None:
                stmt = stmt.where(CostEvent.provider == provider)
            if model is not None:
                stmt = stmt.where(CostEvent.model == model)
            for row in session.scalars(stmt):
                usage = _usage_from_row(row)
                price = price_book.resolve(
                    workspace_id=workspace_id,
                    provider=row.provider,
                    model=row.model,
                    kind=usage.kind,
                    at=row.occurred_at,
                )
                new_cost = compute_cost(usage, price)
                new_price_id = price.id if price is not None else None
                if new_cost != row.cost_usd or new_price_id != row.price_id:
                    row.cost_usd = new_cost
                    row.price_id = new_price_id
                    updated += 1
            session.commit()
        return updated


class SqlCostReader:
    """Workspace-scoped rollups over ``cost_event`` for the Cost API."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _scope_clause(scope: str, scope_id: UUID):
        from forge_db.models.cost import CostEvent

        return {
            "workspace": CostEvent.workspace_id == scope_id,
            "project": CostEvent.project_id == scope_id,
            "task": CostEvent.task_id == scope_id,
        }[scope]

    def summary(
        self,
        *,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostSummary:
        from sqlalchemy import select

        from forge_db.models.cost import CostEvent

        _validate(scope, group_by)
        buckets: dict[str, CostBucket] = {}
        total = Decimal(0)
        prompt = completion = 0
        with self._session_factory() as session:
            stmt = select(CostEvent).where(
                CostEvent.workspace_id == workspace_id,
                self._scope_clause(scope, scope_id),
            )
            if frm is not None:
                stmt = stmt.where(CostEvent.occurred_at >= frm)
            if to is not None:
                stmt = stmt.where(CostEvent.occurred_at < to)
            for row in session.scalars(stmt):
                key = _group_key(
                    group_by, phase=row.phase, provider=row.provider, model=row.model
                )
                entry = buckets.setdefault(key, CostBucket(key=key, cost_usd=Decimal(0)))
                cost = Decimal(row.cost_usd)
                entry.cost_usd += cost
                entry.prompt_tokens += row.prompt_tokens
                entry.completion_tokens += row.completion_tokens
                total += cost
                prompt += row.prompt_tokens
                completion += row.completion_tokens
        return CostSummary(
            scope=scope,
            scope_id=scope_id,
            total_cost_usd=total,
            total_prompt_tokens=prompt,
            total_completion_tokens=completion,
            group_by=group_by,
            buckets=sorted(buckets.values(), key=lambda b: b.key),
            from_=frm,
            to=to,
        )

    def timeseries(
        self,
        *,
        workspace_id: UUID,
        scope: str,
        scope_id: UUID,
        bucket: str,
        group_by: str,
        frm: datetime | None,
        to: datetime | None,
    ) -> CostTimeseries:
        from sqlalchemy import select

        from forge_db.models.cost import CostEvent

        _validate(scope, group_by, bucket)
        series: dict[str, dict[datetime, Decimal]] = {}
        with self._session_factory() as session:
            stmt = select(CostEvent).where(
                CostEvent.workspace_id == workspace_id,
                self._scope_clause(scope, scope_id),
            )
            if frm is not None:
                stmt = stmt.where(CostEvent.occurred_at >= frm)
            if to is not None:
                stmt = stmt.where(CostEvent.occurred_at < to)
            for row in session.scalars(stmt):
                key = _group_key(
                    group_by, phase=row.phase, provider=row.provider, model=row.model
                )
                ts = _truncate(row.occurred_at, bucket)
                per_key = series.setdefault(key, {})
                per_key[ts] = per_key.get(ts, Decimal(0)) + Decimal(row.cost_usd)
        return CostTimeseries(
            scope=scope,
            scope_id=scope_id,
            bucket=bucket,
            group_by=group_by,
            series={k: sorted(v.items()) for k, v in sorted(series.items())},
        )
