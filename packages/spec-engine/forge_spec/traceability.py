"""Requirement -> acceptance -> task -> test traceability + validation reports.

FORGE_SPEC Spec Gating Rules require a validation pass mapped back to acceptance
criteria, and the approval UI must show requirement-to-test traceability. This
module builds those rows from a manifest + its generated tasks, and folds any
*recorded* verification results (lint/type/tests/coverage) into an honest
``passed`` verdict — traceability completeness AND recorded checks must hold.
"""

from __future__ import annotations

from forge_contracts import (
    CheckResult,
    RequirementTrace,
    SpecManifest,
    TaskDTO,
    ValidationReport,
)
from forge_spec.tasks import test_ref_for


def build_traceability(manifest: SpecManifest, tasks: list[TaskDTO]) -> list[RequirementTrace]:
    """Build one :class:`RequirementTrace` per requirement in the manifest.

    A requirement is ``satisfied`` only when it is traceable end-to-end: it has at
    least one acceptance criterion, a task that addresses that criterion, and a
    corresponding test reference.
    """
    rows: list[RequirementTrace] = []
    for requirement in manifest.requirements:
        acceptance_ids = [
            a.id for a in manifest.acceptance_criteria if requirement.id in a.req_refs
        ]
        task_refs = sorted(
            {
                task.key
                for task in tasks
                if task.key and any(a.id in acceptance_ids for a in task.acceptance_criteria)
            }
        )
        test_refs = [test_ref_for(manifest, acceptance_id) for acceptance_id in acceptance_ids]
        satisfied = bool(acceptance_ids and task_refs and test_refs)
        rows.append(
            RequirementTrace(
                requirement_id=requirement.id,
                text=requirement.text,
                acceptance_criteria_ids=acceptance_ids,
                task_refs=task_refs,
                test_refs=test_refs,
                satisfied=satisfied,
            )
        )
    return rows


def build_validation_report(
    *,
    manifest: SpecManifest,
    task: TaskDTO,
    tasks: list[TaskDTO],
    checks: list[CheckResult] | None = None,
    coverage: float | None = None,
) -> ValidationReport:
    """Assemble the :class:`ValidationReport` for a task against its spec."""
    traceability = build_traceability(manifest, tasks)
    checks = checks or []

    traceability_complete = all(row.satisfied for row in traceability) and bool(traceability)
    checks_pass = all(c.passed for c in checks)
    notes: list[str] = []
    if not traceability_complete:
        unsatisfied = [row.requirement_id for row in traceability if not row.satisfied]
        notes.append(f"untraced requirements: {', '.join(unsatisfied) or 'none defined'}")
    if not checks_pass:
        failed = [c.name for c in checks if not c.passed]
        notes.append(f"failed checks: {', '.join(failed)}")

    return ValidationReport(
        task_id=task.key or str(task.id),
        spec_id=manifest.id,
        passed=traceability_complete and checks_pass,
        traceability=traceability,
        checks=checks,
        coverage=coverage,
        notes=notes,
    )


__all__ = ["build_traceability", "build_validation_report"]
