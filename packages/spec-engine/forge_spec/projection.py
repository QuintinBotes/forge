"""The F23 traceability projector + repository ports.

F23 reads F02/F08 truth through narrow :class:`typing.Protocol` ports so it is
testable with doubles (the unit suite uses fakes; a thin adapter wires the real
sync-SQLAlchemy foundation in ``apps/``). The projector rebuilds a spec's
projection *wholesale* on every refresh (no incremental patching), so it is
idempotent: duplicate event deliveries converge (AC #16) and the event-driven
path equals reconcile-from-empty (AC #17).

Foundation deviations: the slice doc places ``ProjectionRepository`` here as a
concrete class; we keep it a Protocol (the concrete sync-SQLAlchemy repo lives in
the data layer / app wiring, which is parked) plus a working
:class:`InMemoryProjectionRepository` for the projector/service unit tests.
"""

from __future__ import annotations

from typing import Protocol

from forge_contracts import SpecManifest, ValidationReport
from forge_spec.dashboard import (
    build_criterion_links,
    compute_spec_rollup,
    verdicts_from_report,
)
from forge_spec.dashboard_schemas import (
    CriterionLinkRecord,
    EvidenceIndex,
    SpecHeader,
    SpecRollupRecord,
)


class EvidencePort(Protocol):
    """Reads per-criterion F08 PR/test/diff evidence for a spec."""

    def evidence_for_spec(self, spec_id: str) -> EvidenceIndex: ...


class SpecSourcePort(Protocol):
    """Reads F02 spec truth (manifest, current version, latest report, header)."""

    def load_manifest(self, spec_id: str) -> SpecManifest: ...

    def current_version(self, spec_id: str) -> int: ...

    def latest_report(self, spec_id: str) -> tuple[ValidationReport | None, int | None]:
        """``(report, report_spec_version)`` — both ``None`` when never validated."""
        ...

    def spec_header(self, spec_id: str) -> SpecHeader: ...

    def project_spec_ids(self, project_id: str) -> list[str]: ...


class ProjectionRepository(Protocol):
    """Persists + reads the two denormalised projection tables."""

    def replace_spec_links(self, spec_id: str, links: list[CriterionLinkRecord]) -> None: ...

    def upsert_rollup(self, rollup: SpecRollupRecord) -> int:
        """Upsert the rollup row, bump ``projection_version``, return the new value."""
        ...

    def get_rollup(self, spec_id: str) -> SpecRollupRecord | None: ...

    def get_projection_version(self, spec_id: str) -> int: ...

    def list_rollups(self, project_id: str) -> list[SpecRollupRecord]: ...

    def get_links(self, spec_id: str) -> list[CriterionLinkRecord]: ...

    def list_links(self, project_id: str) -> list[CriterionLinkRecord]: ...


class NoOpEvidencePort:
    """Pre-F08 evidence port: every spec has no PR/test/diff evidence.

    Cells then show verdict-only evidence (F08's ``pull_request`` table does not
    exist in this foundation).
    """

    def evidence_for_spec(self, spec_id: str) -> EvidenceIndex:
        return EvidenceIndex()


class InMemoryProjectionRepository:
    """An in-memory :class:`ProjectionRepository` for unit tests + golden eval.

    A faithful stand-in for the (parked) sync-SQLAlchemy repo: it bumps a
    monotonic ``projection_version`` per spec on every ``upsert_rollup`` and
    replaces a spec's link rows wholesale.
    """

    def __init__(self) -> None:
        self._links: dict[str, list[CriterionLinkRecord]] = {}
        self._rollups: dict[str, SpecRollupRecord] = {}
        self._versions: dict[str, int] = {}

    def replace_spec_links(self, spec_id: str, links: list[CriterionLinkRecord]) -> None:
        self._links[spec_id] = list(links)

    def upsert_rollup(self, rollup: SpecRollupRecord) -> int:
        version = self._versions.get(rollup.spec_id, 0) + 1
        self._versions[rollup.spec_id] = version
        self._rollups[rollup.spec_id] = rollup
        return version

    def get_rollup(self, spec_id: str) -> SpecRollupRecord | None:
        return self._rollups.get(spec_id)

    def get_projection_version(self, spec_id: str) -> int:
        return self._versions.get(spec_id, 0)

    def list_rollups(self, project_id: str) -> list[SpecRollupRecord]:
        return [r for r in self._rollups.values() if r.project_id == project_id]

    def get_links(self, spec_id: str) -> list[CriterionLinkRecord]:
        return list(self._links.get(spec_id, []))

    def list_links(self, project_id: str) -> list[CriterionLinkRecord]:
        return [
            link
            for links in self._links.values()
            for link in links
            if link.project_id == project_id
        ]


class TraceabilityProjector:
    """Rebuilds a spec's projection rows from F02/F08 source truth."""

    def __init__(
        self,
        specs: SpecSourcePort,
        evidence: EvidencePort,
        repo: ProjectionRepository,
    ) -> None:
        self._specs = specs
        self._evidence = evidence
        self._repo = repo

    def refresh_spec(self, spec_id: str) -> int:
        """Recompute + persist both projection tables for ``spec_id`` wholesale.

        Returns the new ``projection_version`` (AC #1: exactly one rollup row +
        one link row per AC, version bumped by 1).
        """
        manifest = self._specs.load_manifest(spec_id)
        current_version = self._specs.current_version(spec_id)
        report, report_version = self._specs.latest_report(spec_id)
        header = self._specs.spec_header(spec_id)
        evidence = self._evidence.evidence_for_spec(spec_id)

        verdicts = verdicts_from_report(manifest, report)
        cells = build_criterion_links(
            manifest,
            verdicts,
            current_spec_version=current_version,
            report_spec_version=report_version,
            evidence=evidence,
        )
        rollup = compute_spec_rollup(manifest, cells, spec_status=header.spec_status)

        link_records = [
            CriterionLinkRecord(
                workspace_id=header.workspace_id,
                project_id=header.project_id,
                spec_id=header.spec_id,
                spec_key=header.spec_key,
                criterion_ext_id=cell.criterion_id,
                criterion_text=cell.criterion_text,
                requirement_ext_ids=cell.requirement_ids,
                status=cell.status,
                satisfied=cell.satisfied,
                test_refs=cell.test_refs,
                diff_refs=cell.diff_refs,
                task_ids=cell.task_ids,
                pr_numbers=cell.pr_numbers,
                report_spec_version=report_version,
                current_spec_version=current_version,
                last_validated_at=cell.last_validated_at,
            )
            for cell in cells
        ]
        rollup_record = SpecRollupRecord(
            workspace_id=header.workspace_id,
            project_id=header.project_id,
            spec_id=header.spec_id,
            spec_key=header.spec_key,
            spec_name=header.spec_name,
            epic_id=header.epic_id,
            spec_status=header.spec_status,
            total_requirements=rollup.total_requirements,
            covered_requirements=rollup.covered_requirements,
            total_criteria=rollup.total_criteria,
            validated_criteria=rollup.validated_criteria,
            failed_criteria=rollup.failed_criteria,
            uncovered_criteria=rollup.uncovered_criteria,
            claimed_criteria=rollup.claimed_criteria,
            stale_criteria=rollup.stale_criteria,
            requirement_coverage=rollup.requirement_coverage,
            acceptance_criteria_coverage=rollup.acceptance_criteria_coverage,
            uncovered_requirement_ext_ids=rollup.uncovered_requirement_ext_ids,
            validation_status=rollup.validation_status,
            gap_count=rollup.gap_count,
            last_validated_at=rollup.last_validated_at,
        )

        self._repo.replace_spec_links(spec_id, link_records)
        return self._repo.upsert_rollup(rollup_record)

    def reconcile_project(self, project_id: str) -> int:
        """Refresh every spec in the project; return the count refreshed (AC #17)."""
        spec_ids = self._specs.project_spec_ids(project_id)
        for spec_id in spec_ids:
            self.refresh_spec(spec_id)
        return len(spec_ids)


__all__ = [
    "EvidencePort",
    "InMemoryProjectionRepository",
    "NoOpEvidencePort",
    "ProjectionRepository",
    "SpecSourcePort",
    "TraceabilityProjector",
]
