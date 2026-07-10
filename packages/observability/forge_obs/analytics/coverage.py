"""Coverage-over-time: per-repo-per-day snapshot, recompute, and org rollup.

Mirrors the F26 ``sprint_burndown_snapshot`` pattern: ``coverage_snapshot`` is
derived, rebuildable state, idempotently upserted per ``(project_id, repo_id,
snapshot_date)`` by a recompute job that reads a CI coverage report (lines
covered / lines total) — never a source of truth in its own right.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from pydantic import BaseModel

__all__ = [
    "CoverageSnapshotDTO",
    "CoverageTrendPoint",
    "SqlCoverageRepository",
    "compute_coverage_pct",
]


def compute_coverage_pct(lines_covered: int, lines_total: int) -> float:
    """``lines_covered / lines_total`` as a percentage, ``0.0`` when total is 0."""
    if lines_total <= 0:
        return 0.0
    return round((lines_covered / lines_total) * 100, 2)


class CoverageSnapshotDTO(BaseModel):
    """One derived coverage point for a repo on a calendar day."""

    id: UUID | None = None
    project_id: UUID
    repo_id: str
    snapshot_date: date
    lines_covered: int
    lines_total: int
    coverage_pct: float


class CoverageTrendPoint(BaseModel):
    """One workspace-level rollup point (weighted mean across projects/repos)."""

    snapshot_date: date
    lines_covered: int
    lines_total: int
    coverage_pct: float


class SqlCoverageRepository:
    """Recompute-and-upsert + trend/rollup reads over ``coverage_snapshot``."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _dto(row) -> CoverageSnapshotDTO:
        return CoverageSnapshotDTO(
            id=row.id,
            project_id=row.project_id,
            repo_id=row.repo_id,
            snapshot_date=row.snapshot_date,
            lines_covered=row.lines_covered,
            lines_total=row.lines_total,
            coverage_pct=float(row.coverage_pct),
        )

    def recompute(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID,
        repo_id: str,
        snapshot_date: date,
        lines_covered: int,
        lines_total: int,
    ) -> CoverageSnapshotDTO:
        """Idempotent daily upsert: a same-day recompute corrects the row in place."""
        from sqlalchemy import select

        from forge_db.models.obs_analytics import CoverageSnapshot

        pct = compute_coverage_pct(lines_covered, lines_total)
        with self._session_factory() as session:
            row = session.scalars(
                select(CoverageSnapshot).where(
                    CoverageSnapshot.project_id == project_id,
                    CoverageSnapshot.repo_id == repo_id,
                    CoverageSnapshot.snapshot_date == snapshot_date,
                )
            ).first()
            if row is None:
                row = CoverageSnapshot(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    repo_id=repo_id,
                    snapshot_date=snapshot_date,
                    lines_covered=lines_covered,
                    lines_total=lines_total,
                    coverage_pct=pct,
                )
                session.add(row)
            else:
                row.lines_covered = lines_covered
                row.lines_total = lines_total
                row.coverage_pct = pct
            session.commit()
            session.refresh(row)
            return self._dto(row)

    def trend(
        self,
        *,
        project_id: UUID,
        repo_id: str | None = None,
        frm: date | None = None,
        to: date | None = None,
    ) -> list[CoverageSnapshotDTO]:
        from sqlalchemy import select

        from forge_db.models.obs_analytics import CoverageSnapshot

        with self._session_factory() as session:
            stmt = select(CoverageSnapshot).where(CoverageSnapshot.project_id == project_id)
            if repo_id is not None:
                stmt = stmt.where(CoverageSnapshot.repo_id == repo_id)
            if frm is not None:
                stmt = stmt.where(CoverageSnapshot.snapshot_date >= frm)
            if to is not None:
                stmt = stmt.where(CoverageSnapshot.snapshot_date <= to)
            stmt = stmt.order_by(CoverageSnapshot.snapshot_date)
            return [self._dto(row) for row in session.scalars(stmt)]

    def org_rollup(
        self,
        *,
        workspace_id: UUID,
        frm: date | None = None,
        to: date | None = None,
    ) -> list[CoverageTrendPoint]:
        """Per-day weighted-mean coverage across every project/repo in the workspace."""
        from sqlalchemy import select

        from forge_db.models.obs_analytics import CoverageSnapshot

        with self._session_factory() as session:
            stmt = select(CoverageSnapshot).where(CoverageSnapshot.workspace_id == workspace_id)
            if frm is not None:
                stmt = stmt.where(CoverageSnapshot.snapshot_date >= frm)
            if to is not None:
                stmt = stmt.where(CoverageSnapshot.snapshot_date <= to)
            rows = list(session.scalars(stmt))
        return _rollup_by_day(rows)


def _rollup_by_day(rows: Sequence) -> list[CoverageTrendPoint]:
    by_day: dict[date, tuple[int, int]] = {}
    for row in rows:
        covered, total = by_day.get(row.snapshot_date, (0, 0))
        by_day[row.snapshot_date] = (covered + row.lines_covered, total + row.lines_total)
    return [
        CoverageTrendPoint(
            snapshot_date=day,
            lines_covered=covered,
            lines_total=total,
            coverage_pct=compute_coverage_pct(covered, total),
        )
        for day, (covered, total) in sorted(by_day.items())
    ]
