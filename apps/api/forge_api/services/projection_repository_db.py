"""Postgres-backed :class:`~forge_spec.projection.ProjectionRepository` (F23).

:class:`SqlAlchemyProjectionRepository` is a drop-in, durable alternative to
:class:`~forge_spec.projection.InMemoryProjectionRepository` that satisfies the
**same** ``ProjectionRepository`` protocol (``replace_spec_links`` /
``upsert_rollup`` / ``get_rollup`` / ``get_projection_version`` /
``list_rollups`` / ``get_links`` / ``list_links``) — so the F23 composition root
swaps it in behind ``FORGE_PROJECTION_BACKEND=db`` with no behavioural change.
The default stays ``memory`` and the in-memory store remains the unit-test
default (the projection class docstring calls the DB repo the "parked" sync repo;
this is that repo, now built).

It lives in ``apps/api`` (not the ``forge_spec`` package, which is deliberately
Protocol-ported and DB-free — the module docstring says "a thin adapter wires the
real sync-SQLAlchemy foundation in ``apps/``"), exactly like the sibling
:class:`~forge_api.services.approval_repository_db.SqlAlchemyApprovalRepository`.
It maps the two projection DTOs
(:class:`~forge_spec.dashboard_schemas.CriterionLinkRecord` /
:class:`~forge_spec.dashboard_schemas.SpecRollupRecord`) onto the canonical F23
ORM rows (``forge_db.models.TraceabilityCriterionLink`` /
``TraceabilitySpecRollup``, migration ``0014``).

Behaviour parity with the in-memory store is exact and intentional:

* ``replace_spec_links`` rewrites a spec's link rows **wholesale** (delete all
  rows for the spec, then insert the new set) — the DB analogue of the in-memory
  ``self._links[spec_id] = list(links)``;
* ``upsert_rollup`` bumps a **monotonic** ``projection_version`` per spec: it
  ``SELECT ... FOR UPDATE`` locks the existing rollup row so concurrent refreshes
  serialize (the DB analogue of the in-memory GIL-guarded ``+= 1``), returns ``1``
  on the first upsert and ``old + 1`` thereafter — never reused, never regressed;
* ``get_projection_version`` reads the persisted counter (``0`` when the spec has
  no rollup yet, matching the in-memory default);
* reads filter by ``spec_id`` / ``project_id`` only (the protocol carries no
  workspace dimension, mirroring the in-memory store's flat maps) and return
  deterministically ordered rows.

Storage-boundary divergences (shared with every DB-backed repo here; both still
satisfy the same protocol):

* the ids the protocol types as ``str`` are real ``uuid`` columns, so a write
  requires canonical UUID strings and an existing ``workspace`` / ``project`` /
  ``spec_document`` parent (the FKs are real); a read for a non-UUID or absent id
  reads as "absent" (``None`` / ``[]`` / ``0``), exactly as the in-memory store
  returns for an unknown key;
* the ``(spec_id, criterion_ext_id)`` and ``(spec_id)`` unique constraints are
  enforced by the database — the wholesale rewrite/upsert never trips them in the
  projector's normal flow, but a direct duplicate insert raises at the boundary
  rather than silently overwriting;
* the coverage ratios land in ``NUMERIC(5, 4)`` columns, so they round-trip to
  four decimal places (the projector's ratios are already rounded to that grain).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from forge_db.models import TraceabilityCriterionLink, TraceabilitySpecRollup
from forge_spec.dashboard_schemas import CriterionLinkRecord, SpecRollupRecord

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

__all__ = ["SqlAlchemyProjectionRepository"]


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a stored timestamp to timezone-aware UTC (SQLite reads naive)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _maybe_uuid(value: str | None) -> uuid.UUID | None:
    """Parse ``value`` as a UUID, or ``None`` — a read for a non-UUID id is absent."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _link_record(row: TraceabilityCriterionLink) -> CriterionLinkRecord:
    return CriterionLinkRecord(
        workspace_id=str(row.workspace_id),
        project_id=str(row.project_id),
        spec_id=str(row.spec_id),
        spec_key=row.spec_key,
        criterion_ext_id=row.criterion_ext_id,
        criterion_text=row.criterion_text,
        requirement_ext_ids=list(row.requirement_ext_ids or []),
        status=row.status,  # str -> CellStatus (pydantic coercion)
        satisfied=row.satisfied,
        test_refs=list(row.test_refs or []),
        diff_refs=list(row.diff_refs or []),
        task_ids=list(row.task_ids or []),
        pr_numbers=list(row.pr_numbers or []),
        report_spec_version=row.report_spec_version,
        current_spec_version=row.current_spec_version,
        last_validated_at=_aware(row.last_validated_at),
    )


def _rollup_record(row: TraceabilitySpecRollup) -> SpecRollupRecord:
    return SpecRollupRecord(
        workspace_id=str(row.workspace_id),
        project_id=str(row.project_id),
        spec_id=str(row.spec_id),
        spec_key=row.spec_key,
        spec_name=row.spec_name,
        epic_id=str(row.epic_id) if row.epic_id is not None else None,
        spec_status=row.spec_status,
        total_requirements=row.total_requirements,
        covered_requirements=row.covered_requirements,
        total_criteria=row.total_criteria,
        validated_criteria=row.validated_criteria,
        failed_criteria=row.failed_criteria,
        uncovered_criteria=row.uncovered_criteria,
        claimed_criteria=row.claimed_criteria,
        stale_criteria=row.stale_criteria,
        requirement_coverage=float(row.requirement_coverage),
        acceptance_criteria_coverage=float(row.acceptance_criteria_coverage),
        uncovered_requirement_ext_ids=list(row.uncovered_requirement_ext_ids or []),
        validation_status=row.validation_status,  # str -> ValidationStatus
        gap_count=row.gap_count,
        last_validated_at=_aware(row.last_validated_at),
    )


class SqlAlchemyProjectionRepository:
    """A Postgres-backed ``ProjectionRepository`` (F23 traceability projection)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """A session that commits on success and always closes (writes + reads)."""
        session = self._sf()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- writes -------------------------------------------------------------- #

    def replace_spec_links(self, spec_id: str, links: list[CriterionLinkRecord]) -> None:
        """Rewrite ``spec_id``'s link rows wholesale (delete-all, then insert)."""
        spec_uuid = uuid.UUID(spec_id)
        now = _now()
        with self._session() as session:
            session.execute(
                delete(TraceabilityCriterionLink).where(
                    TraceabilityCriterionLink.spec_id == spec_uuid
                )
            )
            session.flush()
            for link in links:
                session.add(
                    TraceabilityCriterionLink(
                        workspace_id=uuid.UUID(link.workspace_id),
                        project_id=uuid.UUID(link.project_id),
                        spec_id=uuid.UUID(link.spec_id),
                        spec_key=link.spec_key,
                        criterion_ext_id=link.criterion_ext_id,
                        criterion_text=link.criterion_text,
                        requirement_ext_ids=list(link.requirement_ext_ids),
                        status=link.status.value,
                        satisfied=link.satisfied,
                        test_refs=list(link.test_refs),
                        diff_refs=list(link.diff_refs),
                        task_ids=list(link.task_ids),
                        pr_numbers=list(link.pr_numbers),
                        report_spec_version=link.report_spec_version,
                        current_spec_version=link.current_spec_version,
                        last_validated_at=link.last_validated_at,
                        refreshed_at=now,
                    )
                )

    def upsert_rollup(self, rollup: SpecRollupRecord) -> int:
        """Upsert the rollup row, bump ``projection_version`` monotonically, return it."""
        spec_uuid = uuid.UUID(rollup.spec_id)
        with self._session() as session:
            row = session.execute(
                select(TraceabilitySpecRollup)
                .where(TraceabilitySpecRollup.spec_id == spec_uuid)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                new_version = 1
                row = TraceabilitySpecRollup(spec_id=spec_uuid)
                session.add(row)
            else:
                new_version = row.projection_version + 1
            self._apply_rollup(row, rollup, new_version)
            return new_version

    @staticmethod
    def _apply_rollup(row: TraceabilitySpecRollup, rollup: SpecRollupRecord, version: int) -> None:
        row.workspace_id = uuid.UUID(rollup.workspace_id)
        row.project_id = uuid.UUID(rollup.project_id)
        row.spec_id = uuid.UUID(rollup.spec_id)
        row.spec_key = rollup.spec_key
        row.spec_name = rollup.spec_name
        row.epic_id = uuid.UUID(rollup.epic_id) if rollup.epic_id else None
        row.spec_status = rollup.spec_status
        row.total_requirements = rollup.total_requirements
        row.covered_requirements = rollup.covered_requirements
        row.total_criteria = rollup.total_criteria
        row.validated_criteria = rollup.validated_criteria
        row.failed_criteria = rollup.failed_criteria
        row.uncovered_criteria = rollup.uncovered_criteria
        row.claimed_criteria = rollup.claimed_criteria
        row.stale_criteria = rollup.stale_criteria
        row.requirement_coverage = rollup.requirement_coverage
        row.acceptance_criteria_coverage = rollup.acceptance_criteria_coverage
        row.uncovered_requirement_ext_ids = list(rollup.uncovered_requirement_ext_ids)
        row.validation_status = rollup.validation_status.value
        row.gap_count = rollup.gap_count
        row.last_validated_at = rollup.last_validated_at
        row.projection_version = version
        row.refreshed_at = _now()

    # -- reads --------------------------------------------------------------- #

    def get_rollup(self, spec_id: str) -> SpecRollupRecord | None:
        spec_uuid = _maybe_uuid(spec_id)
        if spec_uuid is None:
            return None
        with self._session() as session:
            row = session.execute(
                select(TraceabilitySpecRollup).where(TraceabilitySpecRollup.spec_id == spec_uuid)
            ).scalar_one_or_none()
            return _rollup_record(row) if row is not None else None

    def get_projection_version(self, spec_id: str) -> int:
        spec_uuid = _maybe_uuid(spec_id)
        if spec_uuid is None:
            return 0
        with self._session() as session:
            version = session.execute(
                select(TraceabilitySpecRollup.projection_version).where(
                    TraceabilitySpecRollup.spec_id == spec_uuid
                )
            ).scalar_one_or_none()
            return int(version) if version is not None else 0

    def list_rollups(self, project_id: str) -> list[SpecRollupRecord]:
        project_uuid = _maybe_uuid(project_id)
        if project_uuid is None:
            return []
        with self._session() as session:
            rows = (
                session.execute(
                    select(TraceabilitySpecRollup)
                    .where(TraceabilitySpecRollup.project_id == project_uuid)
                    .order_by(TraceabilitySpecRollup.spec_key, TraceabilitySpecRollup.spec_id)
                )
                .scalars()
                .all()
            )
            return [_rollup_record(row) for row in rows]

    def get_links(self, spec_id: str) -> list[CriterionLinkRecord]:
        spec_uuid = _maybe_uuid(spec_id)
        if spec_uuid is None:
            return []
        with self._session() as session:
            rows = (
                session.execute(
                    select(TraceabilityCriterionLink)
                    .where(TraceabilityCriterionLink.spec_id == spec_uuid)
                    .order_by(TraceabilityCriterionLink.criterion_ext_id)
                )
                .scalars()
                .all()
            )
            return [_link_record(row) for row in rows]

    def list_links(self, project_id: str) -> list[CriterionLinkRecord]:
        project_uuid = _maybe_uuid(project_id)
        if project_uuid is None:
            return []
        with self._session() as session:
            rows = (
                session.execute(
                    select(TraceabilityCriterionLink)
                    .where(TraceabilityCriterionLink.project_id == project_uuid)
                    .order_by(
                        TraceabilityCriterionLink.spec_key,
                        TraceabilityCriterionLink.criterion_ext_id,
                    )
                )
                .scalars()
                .all()
            )
            return [_link_record(row) for row in rows]
