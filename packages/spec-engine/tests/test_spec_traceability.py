"""Requirement -> acceptance -> task -> test traceability + validation tests.

Plan Task 1.7: "traceability maps R->A->task". FORGE_SPEC Spec Gating Rules:
validation must map back to acceptance criteria; the approval UI shows
requirement-to-test traceability.
"""

from __future__ import annotations

import uuid

import pytest

from forge_contracts import (
    Requirement,
    RequirementTrace,
    ValidationReport,
)
from forge_spec import FileSpecEngine, spec_id_for_key


@pytest.fixture
def engine(tmp_path) -> FileSpecEngine:
    return FileSpecEngine(tmp_path)


def _requirements() -> list[Requirement]:
    return [
        Requirement(id="R1", text="Add customer search endpoint with pagination"),
        Requirement(id="R2", text="Endpoint must support bearer token authentication"),
    ]


def _approved_spec(engine: FileSpecEngine):
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)
    engine.approve_spec(spec_id)
    return spec_id, manifest


def test_generated_tasks_carry_spec_traceable_acceptance_criteria(engine) -> None:
    spec_id, manifest = _approved_spec(engine)
    tasks = engine.spec_tasks(spec_id)

    # Every acceptance criterion in the spec is referenced by some task.
    spec_ac_ids = {a.id for a in engine.read_manifest(spec_id).acceptance_criteria}
    task_ac_ids = {a.id for t in tasks for a in t.acceptance_criteria}
    assert spec_ac_ids
    assert spec_ac_ids <= task_ac_ids

    # Tasks point back at the spec and carry spec_ref on their criteria.
    for t in tasks:
        assert t.spec_id == spec_id
        for a in t.acceptance_criteria:
            assert a.spec_ref and a.spec_ref.startswith(manifest.id + "/")


def test_validate_builds_requirement_to_task_to_test_traceability(engine) -> None:
    spec_id, _ = _approved_spec(engine)
    tasks = engine.spec_tasks(spec_id)

    report = engine.validate(tasks[0].id)
    assert isinstance(report, ValidationReport)
    assert report.traceability
    assert all(isinstance(row, RequirementTrace) for row in report.traceability)

    by_req = {row.requirement_id: row for row in report.traceability}
    assert set(by_req) == {"R1", "R2"}
    for req_id, row in by_req.items():
        assert row.acceptance_criteria_ids, f"{req_id} has no acceptance criteria"
        assert row.task_refs, f"{req_id} maps to no task"
        assert row.test_refs, f"{req_id} maps to no test"
        assert row.satisfied is True


def test_validate_passes_when_every_requirement_is_traced(engine) -> None:
    spec_id, _ = _approved_spec(engine)
    tasks = engine.spec_tasks(spec_id)
    report = engine.validate(tasks[0].id)
    assert report.passed is True
    assert report.spec_id is not None


def test_validate_marks_unsatisfied_requirement_without_acceptance(engine) -> None:
    # A requirement with no acceptance criteria cannot be satisfied.
    manifest = engine.spec_create(uuid.uuid4(), "Sparse spec", _requirements())
    spec_id = spec_id_for_key(manifest.id)
    # Strip acceptance criteria for R2 only, then re-persist.
    manifest.acceptance_criteria = [
        a for a in manifest.acceptance_criteria if "R2" not in a.req_refs
    ]
    engine.write_manifest(manifest)
    engine.approve_spec(spec_id)

    tasks = engine.spec_tasks(spec_id)
    report = engine.validate(tasks[0].id)

    by_req = {row.requirement_id: row for row in report.traceability}
    assert by_req["R1"].satisfied is True
    assert by_req["R2"].satisfied is False
    assert report.passed is False


def test_validate_writes_validation_md(engine) -> None:
    spec_id, _ = _approved_spec(engine)
    tasks = engine.spec_tasks(spec_id)
    engine.validate(tasks[0].id)
    assert (engine.spec_path(spec_id) / "validation.md").exists()


def test_validate_unknown_task_raises(engine) -> None:
    from forge_spec import SpecNotFoundError

    with pytest.raises(SpecNotFoundError):
        engine.validate(uuid.uuid4())


def test_validate_with_recorded_checks_reflects_results(engine, tmp_path) -> None:
    # When verification results are recorded for the task, validate surfaces them
    # and folds them into ``passed`` (honest gate, not traceability-only).
    spec_id, _ = _approved_spec(engine)
    tasks = engine.spec_tasks(spec_id)
    engine.record_verification(
        tasks[0].id,
        checks=[
            {"name": "lint", "passed": True},
            {"name": "tests", "passed": False, "details": "1 failing"},
        ],
        coverage=72.0,
    )
    report = engine.validate(tasks[0].id)
    names = {c.name: c.passed for c in report.checks}
    assert names == {"lint": True, "tests": False}
    assert report.coverage == 72.0
    # A failing recorded check blocks the validation pass even if traceable.
    assert report.passed is False
