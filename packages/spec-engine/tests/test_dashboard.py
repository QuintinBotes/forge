"""Pure unit tests for the F23 dashboard rollup/classification/gap logic.

No DB: every function under test is deterministic and I/O-free (AC #19). Covers
AC #2-#9, #6-#8, #12, #13 at the pure-logic level.
"""

from __future__ import annotations

import pytest

from forge_contracts import (
    AcceptanceCriterion,
    Requirement,
    RequirementTrace,
    SpecManifest,
    ValidationReport,
)
from forge_spec.dashboard import (
    build_criterion_links,
    build_requirement_rows,
    classify_cell,
    compute_spec_rollup,
    detect_gaps,
    summarize_project,
    verdicts_from_report,
)
from forge_spec.dashboard_schemas import (
    CellStatus,
    CriterionVerdict,
    EvidenceIndex,
    GapKind,
    SpecValidationRow,
    ValidationStatus,
)


def _manifest(*, requirements, criteria) -> SpecManifest:
    return SpecManifest(
        id="SPEC-1",
        name="Customer endpoint",
        requirements=requirements,
        acceptance_criteria=criteria,
    )


# --------------------------------------------------------------------------- #
# classify_cell — AC #2-#5                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdict", "report_v", "current_v", "expected"),
    [
        (None, None, 5, CellStatus.UNCOVERED),
        (CriterionVerdict(criterion_id="A1", satisfied=True, test_refs=("t",)), 5, 5,
         CellStatus.VALIDATED),
        (CriterionVerdict(criterion_id="A1", satisfied=True, test_refs=("t",)), 2, 5,
         CellStatus.STALE),
        (CriterionVerdict(criterion_id="A1", satisfied=False, test_refs=("t",)), 5, 5,
         CellStatus.FAILED),
        (CriterionVerdict(criterion_id="A1", satisfied=False, test_refs=()), 5, 5,
         CellStatus.CLAIMED),
    ],
)
def test_classify_cell(verdict, report_v, current_v, expected) -> None:
    assert (
        classify_cell(
            verdict=verdict, report_spec_version=report_v, current_spec_version=current_v
        )
        is expected
    )


def test_classify_cell_satisfied_current_when_no_report_version() -> None:
    # report_spec_version None + satisfied -> validated (cannot be stale).
    v = CriterionVerdict(criterion_id="A1", satisfied=True, test_refs=("t",))
    assert classify_cell(verdict=v, report_spec_version=None, current_spec_version=3) is (
        CellStatus.VALIDATED
    )


# --------------------------------------------------------------------------- #
# compute_spec_rollup — AC #7, #8, #9                                          #
# --------------------------------------------------------------------------- #


def _cell(cid: str, status: CellStatus, reqs: list[str]) -> object:
    from forge_spec.dashboard_schemas import TraceCell

    return TraceCell(criterion_id=cid, criterion_text=cid, requirement_ids=reqs, status=status)


def test_rollup_all_validated_is_passing_coverage_one() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"])],
    )
    cells = [_cell("A1", CellStatus.VALIDATED, ["R1"])]
    rollup = compute_spec_rollup(manifest, cells, spec_status="validated")
    assert rollup.validation_status is ValidationStatus.PASSING
    assert rollup.requirement_coverage == 1.0
    assert rollup.acceptance_criteria_coverage == 1.0


def test_rollup_one_stale_is_stale() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R1"]),
        ],
    )
    cells = [
        _cell("A1", CellStatus.VALIDATED, ["R1"]),
        _cell("A2", CellStatus.STALE, ["R1"]),
    ]
    rollup = compute_spec_rollup(manifest, cells, spec_status="validated")
    assert rollup.validation_status is ValidationStatus.STALE
    assert rollup.stale_criteria == 1


def test_rollup_one_failed_is_failing() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R1"]),
        ],
    )
    # failed wins over stale.
    cells = [
        _cell("A1", CellStatus.FAILED, ["R1"]),
        _cell("A2", CellStatus.STALE, ["R1"]),
    ]
    rollup = compute_spec_rollup(manifest, cells, spec_status="implementing")
    assert rollup.validation_status is ValidationStatus.FAILING


def test_rollup_mixed_validated_uncovered_is_partial() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1"), Requirement(id="R2", text="r2")],
        criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R2"]),
        ],
    )
    cells = [
        _cell("A1", CellStatus.VALIDATED, ["R1"]),
        _cell("A2", CellStatus.UNCOVERED, ["R2"]),
    ]
    rollup = compute_spec_rollup(manifest, cells, spec_status="implementing")
    assert rollup.validation_status is ValidationStatus.PARTIAL


def test_rollup_zero_requirements_no_divide_by_zero() -> None:
    manifest = _manifest(requirements=[], criteria=[])
    rollup = compute_spec_rollup(manifest, [], spec_status="draft")
    assert rollup.requirement_coverage == 0.0
    assert rollup.acceptance_criteria_coverage == 0.0
    assert rollup.validation_status is ValidationStatus.NONE


def test_rollup_zero_criteria_coverage_zero() -> None:
    manifest = _manifest(requirements=[Requirement(id="R1", text="r1")], criteria=[])
    rollup = compute_spec_rollup(manifest, [], spec_status="draft")
    assert rollup.acceptance_criteria_coverage == 0.0
    assert rollup.requirement_coverage == 0.0  # R1 has no AC -> not covered


def test_requirement_covered_only_when_every_ac_validated() -> None:
    # AC #7 strict definition: a requirement with one claimed AC is NOT covered.
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R1"]),
        ],
    )
    claimed = [
        _cell("A1", CellStatus.VALIDATED, ["R1"]),
        _cell("A2", CellStatus.CLAIMED, ["R1"]),
    ]
    assert compute_spec_rollup(manifest, claimed, spec_status="x").covered_requirements == 0

    both = [
        _cell("A1", CellStatus.VALIDATED, ["R1"]),
        _cell("A2", CellStatus.VALIDATED, ["R1"]),
    ]
    assert compute_spec_rollup(manifest, both, spec_status="x").covered_requirements == 1


# --------------------------------------------------------------------------- #
# detect_gaps — AC #6, #13                                                     #
# --------------------------------------------------------------------------- #


def test_detect_gaps_four_kinds() -> None:
    manifest = _manifest(
        requirements=[
            Requirement(id="R1", text="r1"),
            Requirement(id="R2", text="r2"),  # unreferenced -> REQUIREMENT_NO_CRITERIA
        ],
        criteria=[
            AcceptanceCriterion(id="A1", text="claim", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="fail", req_refs=["R1"]),
            AcceptanceCriterion(id="A3", text="stale", req_refs=["R1"]),
        ],
    )
    cells = [
        _cell("A1", CellStatus.CLAIMED, ["R1"]),
        _cell("A2", CellStatus.FAILED, ["R1"]),
        _cell("A3", CellStatus.STALE, ["R1"]),
    ]
    gaps = detect_gaps(manifest, cells, spec_key="SPEC-1", spec_id="sid")
    kinds = sorted(g.kind for g in gaps)
    assert kinds == sorted(
        [
            GapKind.REQUIREMENT_NO_CRITERIA,
            GapKind.CRITERION_NO_TEST,
            GapKind.CRITERION_FAILED,
            GapKind.CRITERION_STALE,
        ]
    )
    assert all(g.deep_link for g in gaps)


def test_determinism() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"])],
    )
    cells = [_cell("A1", CellStatus.FAILED, ["R1"])]
    assert compute_spec_rollup(manifest, cells, spec_status="x") == compute_spec_rollup(
        manifest, cells, spec_status="x"
    )
    assert detect_gaps(manifest, cells, spec_key="K", spec_id="s") == detect_gaps(
        manifest, cells, spec_key="K", spec_id="s"
    )


# --------------------------------------------------------------------------- #
# matrix — AC #12 (shared AC under two requirement rows)                       #
# --------------------------------------------------------------------------- #


def test_shared_ac_appears_under_both_requirement_rows() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1"), Requirement(id="R3", text="r3")],
        criteria=[AcceptanceCriterion(id="A1", text="shared", req_refs=["R1", "R3"])],
    )
    cells = [_cell("A1", CellStatus.VALIDATED, ["R1", "R3"])]
    rows = build_requirement_rows(manifest, cells)
    by_req = {r.requirement_id: r for r in rows}
    assert by_req["R1"].criteria[0].criterion_id == "A1"
    assert by_req["R3"].criteria[0].criterion_id == "A1"
    assert by_req["R1"].criteria[0] == by_req["R3"].criteria[0]


# --------------------------------------------------------------------------- #
# build_criterion_links — evidence merge + verdict derivation                  #
# --------------------------------------------------------------------------- #


def test_build_criterion_links_merges_verdict_and_evidence() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"])],
    )
    verdicts = {"A1": CriterionVerdict(criterion_id="A1", satisfied=True, test_refs=("t::a1",))}
    evidence = EvidenceIndex(
        test_refs={"A1": ["t::a1", "t::extra"]},
        diff_refs={"A1": ["src/x.py"]},
        pr_numbers={"A1": [42]},
        task_ids={"A1": ["TASK-7"]},
    )
    cells = build_criterion_links(
        manifest, verdicts, current_spec_version=1, report_spec_version=1, evidence=evidence
    )
    cell = cells[0]
    assert cell.status is CellStatus.VALIDATED
    assert cell.test_refs == ["t::a1", "t::extra"]  # deduped merge, order preserved
    assert cell.diff_refs == ["src/x.py"]
    assert cell.pr_numbers == [42]
    assert cell.task_ids == ["TASK-7"]


def test_verdicts_from_report_derives_per_criterion() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[
            AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"]),
            AcceptanceCriterion(id="A2", text="a2", req_refs=["R1"]),
        ],
    )
    report = ValidationReport(
        task_id="TASK-1",
        spec_id="SPEC-1",
        passed=False,
        traceability=[
            RequirementTrace(
                requirement_id="R1",
                acceptance_criteria_ids=["A1", "A2"],
                task_refs=["SPEC-1-T1"],
                test_refs=["t::a1", "t::a2"],
                satisfied=True,
            )
        ],
    )
    verdicts = verdicts_from_report(manifest, report)
    assert verdicts["A1"].satisfied is True
    assert verdicts["A1"].test_refs == ("t::a1",)
    assert verdicts["A2"].test_refs == ("t::a2",)


def test_verdicts_from_report_none_when_no_report() -> None:
    manifest = _manifest(
        requirements=[Requirement(id="R1", text="r1")],
        criteria=[AcceptanceCriterion(id="A1", text="a1", req_refs=["R1"])],
    )
    assert verdicts_from_report(manifest, None) == {}


# --------------------------------------------------------------------------- #
# summarize_project — AC #10                                                   #
# --------------------------------------------------------------------------- #


def _row(**kw) -> SpecValidationRow:
    base = {
        "spec_id": "s",
        "spec_key": "SPEC-1",
        "spec_name": "n",
        "spec_status": "validated",
        "requirement_coverage": 1.0,
        "acceptance_criteria_coverage": 1.0,
        "total_requirements": 2,
        "covered_requirements": 2,
        "total_criteria": 4,
        "validated_criteria": 4,
        "failed_criteria": 0,
        "uncovered_criteria": 0,
        "stale_criteria": 0,
        "validation_status": ValidationStatus.PASSING,
        "gap_count": 0,
    }
    base.update(kw)
    return SpecValidationRow(**base)


def test_summarize_project_totals_equal_sum_of_rows() -> None:
    rows = [
        _row(spec_id="a"),
        _row(
            spec_id="b",
            validation_status=ValidationStatus.PARTIAL,
            validated_criteria=2,
            covered_requirements=1,
            gap_count=3,
            stale_criteria=1,
        ),
    ]
    summary = summarize_project("proj", rows)
    assert summary.spec_count == 2
    assert summary.specs_validated == 1
    assert summary.total_requirements == 4
    assert summary.covered_requirements == 3
    assert summary.total_criteria == 8
    assert summary.validated_criteria == 6
    assert summary.open_gap_count == 3
    assert summary.stale_validation_count == 1
    assert summary.requirement_coverage == 3 / 4
    assert summary.acceptance_criteria_coverage == 6 / 8
