"""SDD lifecycle + gating tests for ``forge_spec`` (plan Task 1.7).

Covers: constitution_init -> spec_create -> spec_clarify -> spec_plan ->
approve -> spec_tasks, the on-disk folder layout (FORGE_SPEC: Spec Folder
Layout), and the spec gate (no task generation / implementation without an
approved spec).
"""

from __future__ import annotations

import uuid

import pytest

from forge_contracts import (
    Constitution,
    Requirement,
    SpecGateError,
    SpecManifest,
    SpecStatus,
    TaskDTO,
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


# --------------------------------------------------------------------------- #
# Constitution                                                                 #
# --------------------------------------------------------------------------- #


def test_constitution_init_returns_constitution_and_writes_file(engine, tmp_path) -> None:
    project_id = uuid.uuid4()
    const = engine.constitution_init(project_id, principles=["Prefer composition", "Test first"])

    assert isinstance(const, Constitution)
    assert const.project_id == project_id
    assert "Prefer composition" in const.principles
    assert (tmp_path / "constitution.md").exists()


def test_constitution_init_defaults_principles_when_none(engine) -> None:
    const = engine.constitution_init(uuid.uuid4())
    assert const.principles  # a non-empty default set is provided


# --------------------------------------------------------------------------- #
# spec_create                                                                  #
# --------------------------------------------------------------------------- #


def test_spec_create_returns_draft_manifest(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())

    assert isinstance(manifest, SpecManifest)
    assert manifest.status is SpecStatus.DRAFT
    assert manifest.id.startswith("SPEC-")
    assert [r.id for r in manifest.requirements] == ["R1", "R2"]
    # An acceptance criterion is auto-derived per requirement (verifiable spec).
    assert {a.id for a in manifest.acceptance_criteria}
    assert all(a.req_refs for a in manifest.acceptance_criteria)


def test_spec_create_writes_spec_md_and_manifest(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_dir = engine.spec_path(spec_id_for_key(manifest.id))

    assert (spec_dir / "spec.md").exists()
    assert (spec_dir / "manifest.yaml").exists()
    # The folder is named "<KEY>-<slug>" per the spec folder layout.
    assert spec_dir.name.startswith(manifest.id + "-")
    assert "customer-endpoint" in spec_dir.name


def test_spec_create_allocates_incrementing_keys(engine) -> None:
    first = engine.spec_create(uuid.uuid4(), "First", _requirements())
    second = engine.spec_create(uuid.uuid4(), "Second", _requirements())
    assert first.id != second.id
    assert second.id == "SPEC-2"


# --------------------------------------------------------------------------- #
# clarify + plan                                                              #
# --------------------------------------------------------------------------- #


def test_full_lifecycle_creates_full_folder_layout(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)

    clarified = engine.spec_clarify(spec_id)
    assert clarified.status is SpecStatus.CLARIFYING
    assert clarified.open_questions  # clarify surfaces open questions

    planned = engine.spec_plan(spec_id)
    assert planned.plan_ref == "plan.md"
    assert planned.decisions  # plan records at least one ADR

    spec_dir = engine.spec_path(spec_id)
    for artifact in ("spec.md", "clarify.md", "plan.md", "decisions.md", "manifest.yaml"):
        assert (spec_dir / artifact).exists(), f"missing artifact {artifact}"


# --------------------------------------------------------------------------- #
# Gate: no tasks / implementation without an approved spec                     #
# --------------------------------------------------------------------------- #


def test_spec_tasks_blocked_when_not_approved(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)
    engine.spec_clarify(spec_id)

    with pytest.raises(SpecGateError):
        engine.spec_tasks(spec_id)


def test_ensure_implementable_blocks_unapproved_spec(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)

    with pytest.raises(SpecGateError):
        engine.ensure_implementable(spec_id)


def test_approve_then_tasks_succeeds(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)
    engine.spec_clarify(spec_id)
    engine.spec_plan(spec_id)

    approved = engine.approve_spec(spec_id)
    assert approved.status is SpecStatus.APPROVED

    tasks = engine.spec_tasks(spec_id)
    assert tasks
    assert all(isinstance(t, TaskDTO) for t in tasks)
    # ensure_implementable no longer raises once approved.
    assert engine.ensure_implementable(spec_id).status is SpecStatus.APPROVED


def test_spec_tasks_writes_tasks_artifacts(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)
    engine.approve_spec(spec_id)
    engine.spec_tasks(spec_id)

    spec_dir = engine.spec_path(spec_id)
    assert (spec_dir / "tasks.md").exists()
    assert (spec_dir / "tasks.yaml").exists()
    assert engine.read_manifest(spec_id).tasks_ref == "tasks.md"


# --------------------------------------------------------------------------- #
# manifest read/write                                                          #
# --------------------------------------------------------------------------- #


def test_read_manifest_after_create(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    again = engine.read_manifest(spec_id_for_key(manifest.id))
    assert again.id == manifest.id
    assert again.name == manifest.name


def test_write_manifest_persists_edits(engine) -> None:
    manifest = engine.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    manifest.constraints = ["Follow existing auth middleware pattern"]
    engine.write_manifest(manifest)

    reloaded = engine.read_manifest(spec_id_for_key(manifest.id))
    assert reloaded.constraints == ["Follow existing auth middleware pattern"]


def test_read_manifest_unknown_spec_raises(engine) -> None:
    from forge_spec import SpecNotFoundError

    with pytest.raises(SpecNotFoundError):
        engine.read_manifest(uuid.uuid4())


def test_engine_recovers_state_from_disk(tmp_path) -> None:
    # A fresh engine over the same root must resolve specs written earlier
    # (no reliance on in-process state).
    eng1 = FileSpecEngine(tmp_path)
    manifest = eng1.spec_create(uuid.uuid4(), "Customer endpoint", _requirements())
    spec_id = spec_id_for_key(manifest.id)

    eng2 = FileSpecEngine(tmp_path)
    assert eng2.read_manifest(spec_id).id == manifest.id
