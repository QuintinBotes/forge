"""DashboardService read-assembly tests.

Covers AC #10 (summary totals), #11 (filters + cursor pagination), #12 (matrix
shape), #13 (gaps == matrix-implied), #18 (CSV/JSON export).
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
from forge_spec.dashboard_schemas import SpecHeader
from forge_spec.dashboard_service import CSV_COLUMNS, DashboardService
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


def _register(
    source: FakeSpecSource,
    *,
    spec_id: str,
    spec_key: str,
    current_version: int,
    report_version: int | None,
    satisfied: bool,
    epic_id: str | None = None,
) -> None:
    manifest = SpecManifest(
        id=spec_key,
        name=f"{spec_key} feature",
        requirements=[Requirement(id="R1", text="r1"), Requirement(id="R2", text="r2")],
        acceptance_criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R2"]),
        ],
    )
    report = None
    if report_version is not None:
        report = ValidationReport(
            task_id="T",
            spec_id=spec_key,
            passed=satisfied,
            traceability=[
                RequirementTrace(
                    requirement_id="R1",
                    acceptance_criteria_ids=["A1"],
                    task_refs=["T1"],
                    test_refs=["t::a1"],
                    satisfied=satisfied,
                ),
                RequirementTrace(
                    requirement_id="R2",
                    acceptance_criteria_ids=["A2"],
                    task_refs=["T2"],
                    test_refs=["t::a2"],
                    satisfied=satisfied,
                ),
            ],
        )
    source.manifests[spec_id] = manifest
    source.versions[spec_id] = current_version
    source.reports[spec_id] = (report, report_version)
    source.headers[spec_id] = SpecHeader(
        spec_id=spec_id,
        workspace_id="ws-1",
        project_id="proj-1",
        spec_key=spec_key,
        spec_name=f"{spec_key} feature",
        spec_status="validated",
        epic_id=epic_id,
    )
    source.project_map.setdefault("proj-1", []).append(spec_id)


def _seed() -> tuple[DashboardService, InMemoryProjectionRepository]:
    source = FakeSpecSource()
    # a: all validated; b: failed (gaps, epic E1); c: stale.
    _register(source, spec_id="a", spec_key="SPEC-1", current_version=1,
              report_version=1, satisfied=True)
    _register(source, spec_id="b", spec_key="SPEC-2", current_version=1,
              report_version=1, satisfied=False, epic_id="E1")
    _register(source, spec_id="c", spec_key="SPEC-3", current_version=3,
              report_version=2, satisfied=True)
    repo = InMemoryProjectionRepository()
    projector = TraceabilityProjector(source, NoOpEvidencePort(), repo)
    projector.reconcile_project("proj-1")
    return DashboardService(repo, source), repo


def test_summary_totals() -> None:
    service, _ = _seed()
    summary = service.get_summary("proj-1")
    assert summary.spec_count == 3
    assert summary.specs_validated == 1  # only spec a passes
    assert summary.total_criteria == 6
    assert summary.validated_criteria == 2  # only spec a (b is failed, c is stale)
    assert summary.stale_validation_count == 2  # c's two stale criteria


def test_list_specs_filters() -> None:
    service, _ = _seed()
    all_rows = service.list_specs("proj-1")
    assert [r.spec_key for r in all_rows.items] == ["SPEC-1", "SPEC-2", "SPEC-3"]

    gaps = service.list_specs("proj-1", has_gaps=True)
    assert {r.spec_key for r in gaps.items} == {"SPEC-2", "SPEC-3"}

    stale = service.list_specs("proj-1", stale=True)
    assert {r.spec_key for r in stale.items} == {"SPEC-3"}

    by_epic = service.list_specs("proj-1", epic_id="E1")
    assert {r.spec_key for r in by_epic.items} == {"SPEC-2"}

    by_q = service.list_specs("proj-1", q="spec-2")  # case-insensitive
    assert {r.spec_key for r in by_q.items} == {"SPEC-2"}


def test_list_specs_pagination() -> None:
    service, _ = _seed()
    page1 = service.list_specs("proj-1", limit=2)
    assert len(page1.items) == 2
    assert page1.next_cursor is not None
    page2 = service.list_specs("proj-1", limit=2, cursor=page1.next_cursor)
    assert len(page2.items) == 1
    assert page2.next_cursor is None


def test_list_specs_limit_capped_at_200() -> None:
    service, _ = _seed()
    page = service.list_specs("proj-1", limit=10_000)
    assert len(page.items) == 3  # only 3 exist; the cap doesn't error


def test_matrix_shape() -> None:
    service, _ = _seed()
    matrix = service.get_matrix("a")
    assert matrix is not None
    assert matrix.spec_key == "SPEC-1"
    assert [row.requirement_id for row in matrix.rows] == ["R1", "R2"]
    assert matrix.rows[0].criteria[0].criterion_id == "A1"
    assert matrix.projection_version >= 1


def test_matrix_missing_spec_returns_none() -> None:
    service, _ = _seed()
    assert service.get_matrix("does-not-exist") is None


def test_gaps_match_matrix() -> None:
    service, _ = _seed()
    # spec b: both AC failed -> two CRITERION_FAILED gaps.
    gaps = service.get_gaps("b")
    assert len(gaps) == 2
    assert all(g.kind.value == "criterion_failed" for g in gaps)
    # spec c: both AC stale -> two CRITERION_STALE gaps.
    cgaps = service.get_gaps("c")
    assert {g.kind.value for g in cgaps} == {"criterion_stale"}


def test_export_csv_one_row_per_criterion() -> None:
    service, _ = _seed()
    csv_text = service.export_csv("proj-1")
    lines = [line for line in csv_text.splitlines() if line]
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert len(lines) == 1 + 6  # header + 3 specs * 2 AC


def test_export_csv_honors_filter() -> None:
    service, _ = _seed()
    csv_text = service.export_csv("proj-1", stale=True)
    lines = [line for line in csv_text.splitlines() if line]
    assert len(lines) == 1 + 2  # header + spec c's 2 criteria


def test_export_json_structure() -> None:
    service, _ = _seed()
    payload = service.export_json("proj-1")
    assert set(payload) == {"summary", "specs"}
    assert payload["summary"]["spec_count"] == 3
    assert len(payload["specs"]) == 3
