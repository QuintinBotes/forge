"""The single read entry point routers call for the F23 dashboard.

``DashboardService`` reads the denormalised projection through a
:class:`~forge_spec.projection.ProjectionRepository` and assembles the §4 DTOs.
It does **not** compute on read (computation happens in the refresh path); the
only source it consults beyond the projection is the F02 manifest, for
requirement text/order in the matrix (the projection stores per-criterion rows,
not the full requirement list).
"""

from __future__ import annotations

import base64
import csv
import io

from forge_spec.dashboard import (
    build_requirement_rows,
    detect_gaps,
    summarize_project,
)
from forge_spec.dashboard_schemas import (
    CellStatus,
    CriterionLinkRecord,
    Page,
    ProjectValidationSummary,
    SpecRollupRecord,
    SpecTraceabilityMatrix,
    SpecValidationRow,
    TraceabilityGap,
    TraceCell,
)
from forge_spec.projection import ProjectionRepository, SpecSourcePort

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: CSV export column order (one row per criterion).
CSV_COLUMNS = (
    "spec_key",
    "requirement_ids",
    "criterion_id",
    "status",
    "satisfied",
    "test_refs",
    "pr_numbers",
    "last_validated_at",
    "stale",
)


def _link_to_cell(link: CriterionLinkRecord) -> TraceCell:
    return TraceCell(
        criterion_id=link.criterion_ext_id,
        criterion_text=link.criterion_text,
        requirement_ids=list(link.requirement_ext_ids),
        status=link.status,
        satisfied=link.satisfied,
        test_refs=list(link.test_refs),
        diff_refs=list(link.diff_refs),
        task_ids=list(link.task_ids),
        pr_numbers=list(link.pr_numbers),
        last_validated_at=link.last_validated_at,
        report_spec_version=link.report_spec_version,
    )


def _rollup_to_row(rollup: SpecRollupRecord) -> SpecValidationRow:
    return SpecValidationRow(
        spec_id=rollup.spec_id,
        spec_key=rollup.spec_key,
        spec_name=rollup.spec_name,
        epic_id=rollup.epic_id,
        spec_status=rollup.spec_status,
        requirement_coverage=rollup.requirement_coverage,
        acceptance_criteria_coverage=rollup.acceptance_criteria_coverage,
        total_requirements=rollup.total_requirements,
        covered_requirements=rollup.covered_requirements,
        total_criteria=rollup.total_criteria,
        validated_criteria=rollup.validated_criteria,
        failed_criteria=rollup.failed_criteria,
        uncovered_criteria=rollup.uncovered_criteria,
        stale_criteria=rollup.stale_criteria,
        validation_status=rollup.validation_status,
        gap_count=rollup.gap_count,
        last_validated_at=rollup.last_validated_at,
    )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except (ValueError, TypeError):
        return 0


class DashboardService:
    """Reads projection rows and assembles dashboard DTOs."""

    def __init__(self, repo: ProjectionRepository, specs: SpecSourcePort) -> None:
        self._repo = repo
        self._specs = specs

    # -- project-level reads ------------------------------------------------- #

    def get_summary(self, project_id: str) -> ProjectValidationSummary:
        rows = [_rollup_to_row(r) for r in self._repo.list_rollups(project_id)]
        return summarize_project(project_id, rows)

    def list_specs(
        self,
        project_id: str,
        *,
        status: str | None = None,
        epic_id: str | None = None,
        has_gaps: bool | None = None,
        stale: bool | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[SpecValidationRow]:
        limit = max(1, min(limit, MAX_LIMIT))
        rows = self._filtered_rows(
            project_id, status=status, epic_id=epic_id, has_gaps=has_gaps, stale=stale, q=q
        )
        rows.sort(key=lambda r: r.spec_key)
        offset = _decode_cursor(cursor)
        window = rows[offset : offset + limit]
        next_cursor = _encode_cursor(offset + limit) if offset + limit < len(rows) else None
        return Page(items=window, next_cursor=next_cursor, total=len(rows))

    def _filtered_rows(
        self,
        project_id: str,
        *,
        status: str | None,
        epic_id: str | None,
        has_gaps: bool | None,
        stale: bool | None,
        q: str | None,
    ) -> list[SpecValidationRow]:
        rows = [_rollup_to_row(r) for r in self._repo.list_rollups(project_id)]
        if status is not None:
            rows = [r for r in rows if r.spec_status == status]
        if epic_id is not None:
            rows = [r for r in rows if r.epic_id == epic_id]
        if has_gaps:
            rows = [r for r in rows if r.gap_count > 0]
        if stale:
            rows = [r for r in rows if r.stale_criteria > 0]
        if q:
            needle = q.casefold()
            rows = [
                r
                for r in rows
                if needle in r.spec_key.casefold() or needle in r.spec_name.casefold()
            ]
        return rows

    # -- spec-level reads ---------------------------------------------------- #

    def get_matrix(self, spec_id: str) -> SpecTraceabilityMatrix | None:
        rollup = self._repo.get_rollup(spec_id)
        if rollup is None:
            return None
        links = self._repo.get_links(spec_id)
        cells = [_link_to_cell(link) for link in links]
        manifest = self._specs.load_manifest(spec_id)
        rows = build_requirement_rows(manifest, cells)
        current_version = (
            links[0].current_spec_version if links else self._specs.current_version(spec_id)
        )
        return SpecTraceabilityMatrix(
            spec_id=rollup.spec_id,
            spec_key=rollup.spec_key,
            spec_name=rollup.spec_name,
            spec_status=rollup.spec_status,
            current_spec_version=current_version,
            requirement_coverage=rollup.requirement_coverage,
            acceptance_criteria_coverage=rollup.acceptance_criteria_coverage,
            validation_status=rollup.validation_status,
            rows=rows,
            last_validated_at=rollup.last_validated_at,
            projection_version=self._repo.get_projection_version(spec_id),
        )

    def get_gaps(self, spec_id: str) -> list[TraceabilityGap]:
        rollup = self._repo.get_rollup(spec_id)
        if rollup is None:
            return []
        links = self._repo.get_links(spec_id)
        cells = [_link_to_cell(link) for link in links]
        manifest = self._specs.load_manifest(spec_id)
        return detect_gaps(manifest, cells, spec_key=rollup.spec_key, spec_id=spec_id)

    # -- export -------------------------------------------------------------- #

    def export_csv(self, project_id: str, **filters: object) -> str:
        rows = self._filtered_rows(
            project_id,
            status=filters.get("status"),  # type: ignore[arg-type]
            epic_id=filters.get("epic_id"),  # type: ignore[arg-type]
            has_gaps=filters.get("has_gaps"),  # type: ignore[arg-type]
            stale=filters.get("stale"),  # type: ignore[arg-type]
            q=filters.get("q"),  # type: ignore[arg-type]
        )
        rows.sort(key=lambda r: r.spec_key)
        spec_ids = {r.spec_id for r in rows}
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for link in sorted(
            (link for link in self._repo.list_links(project_id) if link.spec_id in spec_ids),
            key=lambda link_: (link_.spec_key, link_.criterion_ext_id),
        ):
            writer.writerow(
                (
                    link.spec_key,
                    "|".join(link.requirement_ext_ids),
                    link.criterion_ext_id,
                    link.status.value,
                    str(link.satisfied).lower(),
                    "|".join(link.test_refs),
                    "|".join(str(n) for n in link.pr_numbers),
                    link.last_validated_at.isoformat() if link.last_validated_at else "",
                    str(link.status is CellStatus.STALE).lower(),
                )
            )
        return buffer.getvalue()

    def export_json(self, project_id: str, **filters: object) -> dict:
        rows = self._filtered_rows(
            project_id,
            status=filters.get("status"),  # type: ignore[arg-type]
            epic_id=filters.get("epic_id"),  # type: ignore[arg-type]
            has_gaps=filters.get("has_gaps"),  # type: ignore[arg-type]
            stale=filters.get("stale"),  # type: ignore[arg-type]
            q=filters.get("q"),  # type: ignore[arg-type]
        )
        rows.sort(key=lambda r: r.spec_key)
        summary = summarize_project(project_id, rows)
        specs = []
        for row in rows:
            matrix = self.get_matrix(row.spec_id)
            if matrix is not None:
                specs.append(matrix.model_dump(mode="json"))
        return {"summary": summary.model_dump(mode="json"), "specs": specs}


__all__ = ["CSV_COLUMNS", "DEFAULT_LIMIT", "MAX_LIMIT", "DashboardService"]
