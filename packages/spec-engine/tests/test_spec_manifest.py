"""Manifest (de)serialization tests for ``forge_spec`` (plan Task 1.7).

The ``manifest.yaml`` is the machine-readable spec artifact (FORGE_SPEC: Spec
Manifest Schema). These tests pin the on-disk schema keys and the round-trip
``SpecManifest`` <-> YAML so the engine and external tooling agree byte-for-byte.
"""

from __future__ import annotations

import uuid

import yaml

from forge_contracts import (
    ADR,
    AcceptanceCriterion,
    ExecutionMode,
    Requirement,
    SpecManifest,
    SpecStatus,
)
from forge_spec import dump_manifest, load_manifest


def _sample_manifest() -> SpecManifest:
    return SpecManifest(
        id="SPEC-17",
        name="Customer endpoint improvements",
        status=SpecStatus.APPROVED,
        constitution_refs=["engineering/api-principles"],
        repos=["github.com/org/api"],
        requirements=[
            Requirement(id="R1", text="Add customer search endpoint"),
            Requirement(id="R2", text="Endpoint must support bearer auth"),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(id="A1", req_refs=["R1"], text="cursor + limit params"),
            AcceptanceCriterion(id="A2", req_refs=["R2"], text="401 without bearer token"),
        ],
        constraints=["No breaking changes before v2"],
        decisions=[ADR(id="ADR-1", title="Use cursor pagination", status="accepted")],
        plan_ref="plan.md",
        tasks_ref="tasks.md",
        validation_ref="validation.md",
        execution_mode=ExecutionMode.SINGLE_AGENT,
        skill_profile="backend-tdd",
    )


def test_dump_manifest_is_valid_yaml_with_spec_schema_keys() -> None:
    text = dump_manifest(_sample_manifest())
    data = yaml.safe_load(text)

    # Spec Manifest Schema top-level keys (FORGE_SPEC).
    for key in (
        "id",
        "name",
        "status",
        "constitution_refs",
        "repos",
        "requirements",
        "acceptance_criteria",
        "constraints",
        "plan_ref",
        "tasks_ref",
        "validation_ref",
        "execution_mode",
        "skill_profile",
    ):
        assert key in data, f"manifest yaml missing key {key!r}"

    assert data["id"] == "SPEC-17"
    assert data["status"] == "approved"  # StrEnum serialises to its wire value
    assert data["execution_mode"] == "single_agent"
    assert data["requirements"][0] == {"id": "R1", "text": "Add customer search endpoint"}
    assert data["acceptance_criteria"][0]["req_refs"] == ["R1"]


def test_manifest_round_trips_through_yaml() -> None:
    original = _sample_manifest()
    again = load_manifest(dump_manifest(original))

    assert isinstance(again, SpecManifest)
    assert again == original
    assert again.status is SpecStatus.APPROVED
    assert [r.id for r in again.requirements] == ["R1", "R2"]
    assert again.decisions[0].title == "Use cursor pagination"


def test_load_manifest_accepts_plain_dict_text() -> None:
    text = "id: SPEC-9\nname: Minimal\nstatus: draft\n"
    manifest = load_manifest(text)
    assert manifest.id == "SPEC-9"
    assert manifest.status is SpecStatus.DRAFT
    # Defaulted collections are present and empty.
    assert manifest.requirements == []
    assert manifest.acceptance_criteria == []


def test_review_statuses_and_note_round_trip_through_yaml() -> None:
    # Reject / request-changes decisions live in the manifest (file-based
    # engine), so the new statuses + note must survive dump/load.
    for status, note in (
        (SpecStatus.REJECTED, "Missing offline handling"),
        (SpecStatus.CHANGES_REQUESTED, "Please add a rate limit"),
    ):
        original = SpecManifest(id="SPEC-5", name="Review me", status=status, review_note=note)
        again = load_manifest(dump_manifest(original))
        assert again.status is status
        assert again.review_note == note
        assert again == original


def test_load_manifest_without_review_note_defaults_to_none() -> None:
    # Pre-existing manifests (written before review decisions existed) load fine.
    manifest = load_manifest("id: SPEC-9\nname: Minimal\nstatus: draft\n")
    assert manifest.review_note is None


def test_dump_manifest_status_enum_is_a_string_not_python_repr() -> None:
    # Guards against ``!!python/object`` leaking into the YAML for StrEnum fields.
    text = dump_manifest(SpecManifest(id="SPEC-1", name="x", status=SpecStatus.DRAFT))
    assert "!!python" not in text
    assert "status: draft" in text


def test_spec_id_for_key_is_deterministic_uuid() -> None:
    from forge_spec import spec_id_for_key

    a = spec_id_for_key("SPEC-17")
    b = spec_id_for_key("SPEC-17")
    c = spec_id_for_key("SPEC-18")
    assert isinstance(a, uuid.UUID)
    assert a == b
    assert a != c
