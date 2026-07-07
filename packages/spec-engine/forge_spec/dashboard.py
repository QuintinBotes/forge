"""Pure rollup / classification / staleness / gap logic for the F23 dashboard.

No I/O, no DB session: every function is deterministic — identical inputs yield
identical outputs (AC #19). The projector (:mod:`forge_spec.projection`) wires
these onto persisted source rows; routers/services only *read* the projection.

Foundation deviations (conformed to the real foundation, see
:mod:`forge_spec.dashboard_schemas`):

* The persisted :class:`~forge_contracts.ValidationReport` is requirement-grained
  (a list of :class:`~forge_contracts.RequirementTrace`), so per-acceptance-
  criterion verdicts are *derived* via :func:`verdicts_from_report`.
* ``build_criterion_links`` takes the derived ``verdicts`` mapping rather than the
  raw report (cleaner + directly unit-testable); ``classify_cell`` drops the
  unused ``criterion_id`` kwarg from the slice doc's idealised signature.
"""

from __future__ import annotations

from datetime import datetime

from forge_contracts import SpecManifest, ValidationReport
from forge_spec.dashboard_schemas import (
    CellStatus,
    CriterionVerdict,
    EvidenceIndex,
    GapKind,
    ProjectValidationSummary,
    RequirementTraceRow,
    SpecRollupValues,
    SpecValidationRow,
    TraceabilityGap,
    TraceCell,
    ValidationStatus,
)

# Requirement-row rollup chip: pick the first present (worst first), else
# VALIDATED when every cell validated, else UNCOVERED when there are no cells.
_WORST_ORDER: tuple[CellStatus, ...] = (
    CellStatus.FAILED,
    CellStatus.STALE,
    CellStatus.UNCOVERED,
    CellStatus.CLAIMED,
    CellStatus.VALIDATED,
)


def classify_cell(
    *,
    verdict: CriterionVerdict | None,
    report_spec_version: int | None,
    current_spec_version: int,
) -> CellStatus:
    """Classify one acceptance criterion into a :class:`CellStatus`.

    * ``None`` verdict -> ``UNCOVERED`` (no report references this AC).
    * satisfied & report built against the current version -> ``VALIDATED``.
    * satisfied & ``report_spec_version < current_spec_version`` -> ``STALE``.
    * not satisfied & has a test ref (attempted) -> ``FAILED``.
    * not satisfied & no test ref (claim only) -> ``CLAIMED``.
    """
    if verdict is None:
        return CellStatus.UNCOVERED
    if verdict.satisfied:
        if report_spec_version is not None and report_spec_version < current_spec_version:
            return CellStatus.STALE
        return CellStatus.VALIDATED
    if verdict.test_refs:
        return CellStatus.FAILED
    return CellStatus.CLAIMED


def verdicts_from_report(
    manifest: SpecManifest, report: ValidationReport | None
) -> dict[str, CriterionVerdict]:
    """Derive per-acceptance-criterion verdicts from a requirement-grained report.

    For each acceptance criterion ``a`` we gather the report's
    :class:`~forge_contracts.RequirementTrace` rows for the requirements ``a``
    references. ``a`` is *attempted* when at least one such trace exists; its
    ``test_refs`` are the trace test refs aligned to ``a`` (by the trace's
    ``acceptance_criteria_ids``/``test_refs`` index); ``satisfied`` requires the
    AC to have a test ref **and** every referencing requirement trace to be
    satisfied. Criteria not referenced by any trace get no verdict (uncovered).
    """
    if report is None:
        return {}
    traces_by_req = {t.requirement_id: t for t in report.traceability}
    verdicts: dict[str, CriterionVerdict] = {}
    for criterion in manifest.acceptance_criteria:
        refs = [traces_by_req[r] for r in criterion.req_refs if r in traces_by_req]
        if not refs:
            continue
        test_refs: list[str] = []
        for trace in refs:
            ids = trace.acceptance_criteria_ids
            if criterion.id in ids:
                idx = ids.index(criterion.id)
                if idx < len(trace.test_refs):
                    ref = trace.test_refs[idx]
                    if ref and ref not in test_refs:
                        test_refs.append(ref)
        satisfied = bool(test_refs) and all(t.satisfied for t in refs)
        verdicts[criterion.id] = CriterionVerdict(
            criterion_id=criterion.id,
            satisfied=satisfied,
            test_refs=tuple(test_refs),
        )
    return verdicts


def _merge(*lists: list[str]) -> list[str]:
    """Order-preserving dedup merge of string lists."""
    seen: dict[str, None] = {}
    for items in lists:
        for item in items:
            if item not in seen:
                seen[item] = None
    return list(seen)


def _merge_ints(*lists: list[int]) -> list[int]:
    seen: dict[int, None] = {}
    for items in lists:
        for item in items:
            if item not in seen:
                seen[item] = None
    return list(seen)


def build_criterion_links(
    manifest: SpecManifest,
    verdicts: dict[str, CriterionVerdict],
    *,
    current_spec_version: int,
    report_spec_version: int | None,
    evidence: EvidenceIndex | None = None,
    last_validated_at: datetime | None = None,
) -> list[TraceCell]:
    """Build one :class:`TraceCell` per manifest acceptance criterion.

    Merges F02 verdict ``test_refs`` with F08 ``EvidenceIndex`` PR/test/diff
    evidence (deduped) for the same criterion id.
    """
    evidence = evidence or EvidenceIndex()
    cells: list[TraceCell] = []
    for criterion in manifest.acceptance_criteria:
        verdict = verdicts.get(criterion.id)
        status = classify_cell(
            verdict=verdict,
            report_spec_version=report_spec_version,
            current_spec_version=current_spec_version,
        )
        verdict_tests = list(verdict.test_refs) if verdict else []
        cells.append(
            TraceCell(
                criterion_id=criterion.id,
                criterion_text=criterion.text,
                requirement_ids=list(criterion.req_refs),
                status=status,
                satisfied=bool(verdict and verdict.satisfied),
                test_refs=_merge(verdict_tests, evidence.test_refs.get(criterion.id, [])),
                diff_refs=_merge(evidence.diff_refs.get(criterion.id, [])),
                task_ids=_merge(evidence.task_ids.get(criterion.id, [])),
                pr_numbers=_merge_ints(evidence.pr_numbers.get(criterion.id, [])),
                last_validated_at=last_validated_at if verdict else None,
                report_spec_version=report_spec_version if verdict else None,
            )
        )
    return cells


def _requirement_is_covered(req_id: str, cells: list[TraceCell]) -> bool:
    """A requirement is covered iff it has >=1 referencing AC and every one is validated."""
    refs = [c for c in cells if req_id in c.requirement_ids]
    return bool(refs) and all(c.status is CellStatus.VALIDATED for c in refs)


def compute_spec_rollup(
    manifest: SpecManifest, cells: list[TraceCell], *, spec_status: str
) -> SpecRollupValues:
    """Compute the per-spec rollup numbers (coverage, counts, status, gaps)."""
    total_requirements = len(manifest.requirements)
    total_criteria = len(cells)

    validated = sum(1 for c in cells if c.status is CellStatus.VALIDATED)
    failed = sum(1 for c in cells if c.status is CellStatus.FAILED)
    uncovered = sum(1 for c in cells if c.status is CellStatus.UNCOVERED)
    claimed = sum(1 for c in cells if c.status is CellStatus.CLAIMED)
    stale = sum(1 for c in cells if c.status is CellStatus.STALE)

    covered_requirements = sum(
        1 for r in manifest.requirements if _requirement_is_covered(r.id, cells)
    )
    referenced = {rid for c in cells for rid in c.requirement_ids}
    uncovered_requirement_ext_ids = [r.id for r in manifest.requirements if r.id not in referenced]

    requirement_coverage = covered_requirements / total_requirements if total_requirements else 0.0
    acceptance_criteria_coverage = validated / total_criteria if total_criteria else 0.0

    validation_status = _resolve_validation_status(
        failed=failed, stale=stale, validated=validated, total=total_criteria
    )

    gap_count = len(uncovered_requirement_ext_ids) + claimed + failed + stale
    last_validated_at = max(
        (c.last_validated_at for c in cells if c.last_validated_at is not None),
        default=None,
    )

    return SpecRollupValues(
        total_requirements=total_requirements,
        covered_requirements=covered_requirements,
        total_criteria=total_criteria,
        validated_criteria=validated,
        failed_criteria=failed,
        uncovered_criteria=uncovered,
        claimed_criteria=claimed,
        stale_criteria=stale,
        requirement_coverage=requirement_coverage,
        acceptance_criteria_coverage=acceptance_criteria_coverage,
        uncovered_requirement_ext_ids=uncovered_requirement_ext_ids,
        validation_status=validation_status,
        gap_count=gap_count,
        last_validated_at=last_validated_at,
    )


def _resolve_validation_status(
    *, failed: int, stale: int, validated: int, total: int
) -> ValidationStatus:
    """Resolve the per-spec validation status (AC #9)."""
    if failed:
        return ValidationStatus.FAILING
    if stale:
        return ValidationStatus.STALE
    if total and validated == total:
        return ValidationStatus.PASSING
    if validated == 0:
        return ValidationStatus.NONE
    return ValidationStatus.PARTIAL


def requirement_rollup_status(req_id: str, cells: list[TraceCell]) -> CellStatus:
    """Worst-of the cells referencing ``req_id`` (UNCOVERED if it has none)."""
    refs = [c for c in cells if req_id in c.requirement_ids]
    if not refs:
        return CellStatus.UNCOVERED
    statuses = {c.status for c in refs}
    if statuses == {CellStatus.VALIDATED}:
        return CellStatus.VALIDATED
    for status in _WORST_ORDER:
        if status in statuses:
            return status
    return CellStatus.VALIDATED


def build_requirement_rows(
    manifest: SpecManifest, cells: list[TraceCell]
) -> list[RequirementTraceRow]:
    """One :class:`RequirementTraceRow` per requirement; an AC under multiple
    requirements appears (identically) under each (AC #12)."""
    cells_by_id = {c.criterion_id: c for c in cells}
    rows: list[RequirementTraceRow] = []
    for requirement in manifest.requirements:
        row_cells = [
            cells_by_id[a.id]
            for a in manifest.acceptance_criteria
            if requirement.id in a.req_refs and a.id in cells_by_id
        ]
        rows.append(
            RequirementTraceRow(
                requirement_id=requirement.id,
                requirement_text=requirement.text,
                rollup_status=requirement_rollup_status(requirement.id, cells),
                criteria=row_cells,
            )
        )
    return rows


def detect_gaps(
    manifest: SpecManifest,
    cells: list[TraceCell],
    *,
    spec_key: str,
    spec_id: str,
) -> list[TraceabilityGap]:
    """Detect every actionable gap implied by the matrix (AC #6, #13)."""
    gaps: list[TraceabilityGap] = []
    referenced = {rid for c in cells for rid in c.requirement_ids}
    for requirement in manifest.requirements:
        if requirement.id not in referenced:
            gaps.append(
                TraceabilityGap(
                    spec_id=spec_id,
                    spec_key=spec_key,
                    kind=GapKind.REQUIREMENT_NO_CRITERIA,
                    requirement_id=requirement.id,
                    detail=f"{requirement.id} has no acceptance criteria",
                    deep_link=f"/specs/{spec_id}/validation?requirement={requirement.id}",
                )
            )
    for cell in cells:
        if cell.status is CellStatus.CLAIMED:
            gaps.append(
                _criterion_gap(
                    spec_id,
                    spec_key,
                    cell,
                    GapKind.CRITERION_NO_TEST,
                    f"{cell.criterion_id} has an agent claim but no passing test",
                )
            )
        elif cell.status is CellStatus.FAILED:
            gaps.append(
                _criterion_gap(
                    spec_id,
                    spec_key,
                    cell,
                    GapKind.CRITERION_FAILED,
                    f"{cell.criterion_id} failed its latest validation",
                )
            )
        elif cell.status is CellStatus.STALE:
            version = cell.report_spec_version
            gaps.append(
                _criterion_gap(
                    spec_id,
                    spec_key,
                    cell,
                    GapKind.CRITERION_STALE,
                    f"{cell.criterion_id} validated against v{version}, spec has advanced (stale)",
                )
            )
    return gaps


def _criterion_gap(
    spec_id: str, spec_key: str, cell: TraceCell, kind: GapKind, detail: str
) -> TraceabilityGap:
    return TraceabilityGap(
        spec_id=spec_id,
        spec_key=spec_key,
        kind=kind,
        requirement_id=cell.requirement_ids[0] if cell.requirement_ids else None,
        criterion_id=cell.criterion_id,
        detail=detail,
        deep_link=f"/specs/{spec_id}/validation?criterion={cell.criterion_id}",
    )


def summarize_project(project_id: str, rows: list[SpecValidationRow]) -> ProjectValidationSummary:
    """Aggregate per-spec rollup rows into the project summary (AC #10)."""
    total_requirements = sum(r.total_requirements for r in rows)
    covered_requirements = sum(r.covered_requirements for r in rows)
    total_criteria = sum(r.total_criteria for r in rows)
    validated_criteria = sum(r.validated_criteria for r in rows)
    open_gap_count = sum(r.gap_count for r in rows)
    stale_validation_count = sum(r.stale_criteria for r in rows)
    specs_validated = sum(1 for r in rows if r.validation_status is ValidationStatus.PASSING)

    return ProjectValidationSummary(
        project_id=project_id,
        spec_count=len(rows),
        specs_validated=specs_validated,
        total_requirements=total_requirements,
        covered_requirements=covered_requirements,
        total_criteria=total_criteria,
        validated_criteria=validated_criteria,
        requirement_coverage=(
            covered_requirements / total_requirements if total_requirements else 0.0
        ),
        acceptance_criteria_coverage=(
            validated_criteria / total_criteria if total_criteria else 0.0
        ),
        open_gap_count=open_gap_count,
        stale_validation_count=stale_validation_count,
    )


__all__ = [
    "build_criterion_links",
    "build_requirement_rows",
    "classify_cell",
    "compute_spec_rollup",
    "detect_gaps",
    "requirement_rollup_status",
    "summarize_project",
    "verdicts_from_report",
]
