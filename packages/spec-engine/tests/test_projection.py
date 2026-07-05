"""Projector tests against fakes + the in-memory repository.

Covers AC #1 (one rollup + one link per AC, version bumped), #3/#15 (staleness
lifecycle), #16 (idempotency), #17 (reconcile-from-empty == event-driven).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_contracts import (
    AcceptanceCriterion,
    Requirement,
    RequirementTrace,
    SpecManifest,
    ValidationReport,
)
from forge_spec.dashboard_schemas import CellStatus, SpecHeader
from forge_spec.projection import (
    InMemoryProjectionRepository,
    NoOpEvidencePort,
    TraceabilityProjector,
)


@dataclass
class FakeSpecSource:
    manifests: dict[str, SpecManifest] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    reports: dict[str, tuple[ValidationReport | None, int | None]] = field(default_factory=dict)
    headers: dict[str, SpecHeader] = field(default_factory=dict)
    project_map: dict[str, list[str]] = field(default_factory=dict)

    def load_manifest(self, spec_id: str) -> SpecManifest:
        return self.manifests[spec_id]

    def current_version(self, spec_id: str) -> int:
        return self.versions.get(spec_id, 1)

    def latest_report(self, spec_id: str) -> tuple[ValidationReport | None, int | None]:
        return self.reports.get(spec_id, (None, None))

    def spec_header(self, spec_id: str) -> SpecHeader:
        return self.headers[spec_id]

    def project_spec_ids(self, project_id: str) -> list[str]:
        return self.project_map.get(project_id, [])


def _manifest() -> SpecManifest:
    return SpecManifest(
        id="SPEC-1",
        name="Customer endpoint",
        requirements=[Requirement(id="R1", text="r1"), Requirement(id="R2", text="r2")],
        acceptance_criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R1"]),
            AcceptanceCriterion(id="A3", text="a3", req_refs=["R2"]),
        ],
    )


def _report(*, version: int, satisfied: bool) -> ValidationReport:
    return ValidationReport(
        task_id="TASK-1",
        spec_id="SPEC-1",
        passed=satisfied,
        traceability=[
            RequirementTrace(
                requirement_id="R1",
                acceptance_criteria_ids=["A1", "A2"],
                task_refs=["SPEC-1-T1"],
                test_refs=["t::a1", "t::a2"],
                satisfied=satisfied,
            ),
            RequirementTrace(
                requirement_id="R2",
                acceptance_criteria_ids=["A3"],
                task_refs=["SPEC-1-T2"],
                test_refs=["t::a3"],
                satisfied=satisfied,
            ),
        ],
    )


def _header() -> SpecHeader:
    return SpecHeader(
        spec_id="spec-uuid",
        workspace_id="ws-1",
        project_id="proj-1",
        spec_key="SPEC-1",
        spec_name="Customer endpoint",
        spec_status="validated",
        epic_id=None,
    )


def _source(*, version: int, report_version: int | None, satisfied: bool) -> FakeSpecSource:
    report = _report(version=report_version, satisfied=satisfied) if report_version else None
    return FakeSpecSource(
        manifests={"spec-uuid": _manifest()},
        versions={"spec-uuid": version},
        reports={"spec-uuid": (report, report_version)},
        headers={"spec-uuid": _header()},
        project_map={"proj-1": ["spec-uuid"]},
    )


def test_refresh_writes_one_row_per_ac_and_bumps_version() -> None:
    repo = InMemoryProjectionRepository()
    projector = TraceabilityProjector(
        _source(version=1, report_version=1, satisfied=True), NoOpEvidencePort(), repo
    )
    new_version = projector.refresh_spec("spec-uuid")
    assert new_version == 1
    assert len(repo.get_links("spec-uuid")) == 3  # one per AC
    rollup = repo.get_rollup("spec-uuid")
    assert rollup is not None
    assert rollup.total_criteria == 3
    assert rollup.validated_criteria == 3


def test_refresh_is_idempotent_except_version() -> None:
    repo = InMemoryProjectionRepository()
    projector = TraceabilityProjector(
        _source(version=1, report_version=1, satisfied=True), NoOpEvidencePort(), repo
    )
    projector.refresh_spec("spec-uuid")
    first = repo.get_rollup("spec-uuid")
    first_links = repo.get_links("spec-uuid")
    v2 = projector.refresh_spec("spec-uuid")
    second = repo.get_rollup("spec-uuid")
    second_links = repo.get_links("spec-uuid")
    assert v2 == 2  # version increments
    assert first == second  # content identical (records carry no version/timestamp)
    assert first_links == second_links


def test_staleness_lifecycle() -> None:
    repo = InMemoryProjectionRepository()
    # Report built at v2, but the spec has advanced to v3 -> stale.
    projector = TraceabilityProjector(
        _source(version=3, report_version=2, satisfied=True), NoOpEvidencePort(), repo
    )
    projector.refresh_spec("spec-uuid")
    rollup = repo.get_rollup("spec-uuid")
    assert rollup.stale_criteria == 3
    assert all(link.status is CellStatus.STALE for link in repo.get_links("spec-uuid"))

    # Re-validate at v3 -> cells clear to validated, stale count drops (AC #15).
    projector_fresh = TraceabilityProjector(
        _source(version=3, report_version=3, satisfied=True), NoOpEvidencePort(), repo
    )
    projector_fresh.refresh_spec("spec-uuid")
    rollup = repo.get_rollup("spec-uuid")
    assert rollup.stale_criteria == 0
    assert all(link.status is CellStatus.VALIDATED for link in repo.get_links("spec-uuid"))


def test_failed_report_classifies_failed() -> None:
    repo = InMemoryProjectionRepository()
    projector = TraceabilityProjector(
        _source(version=1, report_version=1, satisfied=False), NoOpEvidencePort(), repo
    )
    projector.refresh_spec("spec-uuid")
    rollup = repo.get_rollup("spec-uuid")
    # Unsatisfied verdicts with test refs -> failed.
    assert rollup.failed_criteria == 3
    assert rollup.validation_status.value == "failing"


def test_reconcile_from_empty_equals_event_driven() -> None:
    # Event-driven path (one repo) vs reconcile-from-empty (fresh repo): identical.
    source = _source(version=2, report_version=1, satisfied=True)

    event_repo = InMemoryProjectionRepository()
    TraceabilityProjector(source, NoOpEvidencePort(), event_repo).refresh_spec("spec-uuid")

    reconcile_repo = InMemoryProjectionRepository()
    count = TraceabilityProjector(source, NoOpEvidencePort(), reconcile_repo).reconcile_project(
        "proj-1"
    )

    assert count == 1
    assert event_repo.get_rollup("spec-uuid") == reconcile_repo.get_rollup("spec-uuid")
    assert event_repo.get_links("spec-uuid") == reconcile_repo.get_links("spec-uuid")


def test_uncovered_when_no_report() -> None:
    repo = InMemoryProjectionRepository()
    projector = TraceabilityProjector(
        _source(version=1, report_version=None, satisfied=False), NoOpEvidencePort(), repo
    )
    projector.refresh_spec("spec-uuid")
    rollup = repo.get_rollup("spec-uuid")
    assert rollup.uncovered_criteria == 3
    assert rollup.validation_status.value == "none"
