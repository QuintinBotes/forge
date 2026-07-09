"""Tests for ``forge_spec.diff`` (ss-versioning: spec version diffing)."""

from __future__ import annotations

from forge_contracts import AcceptanceCriterion, Requirement, SpecManifest, SpecStatus
from forge_spec.diff import diff_manifest, diff_markdown


def _manifest(**overrides: object) -> SpecManifest:
    base = {
        "id": "SPEC-1-widget",
        "name": "Widget",
        "status": SpecStatus.DRAFT,
        "requirements": [Requirement(id="R1", text="Do the thing")],
        "acceptance_criteria": [
            AcceptanceCriterion(id="A1", req_refs=["R1"], text="Given...When...Then...")
        ],
        "constraints": ["Must be fast"],
    }
    base.update(overrides)
    return SpecManifest(**base)  # type: ignore[arg-type]


def test_diff_markdown_no_changes_is_all_equal() -> None:
    text = "line one\nline two\n"
    lines = diff_markdown(text, text)
    assert all(line.op == "equal" for line in lines)
    assert [line.text for line in lines] == ["line one", "line two"]


def test_diff_markdown_detects_insert_and_delete() -> None:
    old = "keep\nremove me\n"
    new = "keep\nadd me\n"
    lines = diff_markdown(old, new)
    ops = {(line.op, line.text) for line in lines}
    assert ("equal", "keep") in ops
    assert ("delete", "remove me") in ops
    assert ("insert", "add me") in ops


def test_diff_manifest_no_changes_has_no_changes() -> None:
    manifest = _manifest()
    diff = diff_manifest(manifest, manifest)
    assert diff.has_changes is False
    assert diff.scalar_changes == []
    assert diff.requirements == []


def test_diff_manifest_detects_scalar_change() -> None:
    old = _manifest(status=SpecStatus.DRAFT)
    new = _manifest(status=SpecStatus.APPROVED)
    diff = diff_manifest(old, new)
    assert diff.has_changes is True
    assert len(diff.scalar_changes) == 1
    change = diff.scalar_changes[0]
    assert change.field == "status"
    assert change.before == SpecStatus.DRAFT.value or change.before == SpecStatus.DRAFT
    assert change.after == SpecStatus.APPROVED.value or change.after == SpecStatus.APPROVED


def test_diff_manifest_detects_requirement_added_removed_modified() -> None:
    old = _manifest(
        requirements=[
            Requirement(id="R1", text="Original text"),
            Requirement(id="R2", text="Will be removed"),
        ]
    )
    new = _manifest(
        requirements=[
            Requirement(id="R1", text="Changed text"),
            Requirement(id="R3", text="Brand new"),
        ]
    )
    diff = diff_manifest(old, new)
    by_id = {c.id: c for c in diff.requirements}
    assert by_id["R2"].change == "removed"
    assert by_id["R3"].change == "added"
    assert by_id["R1"].change == "modified"
    assert by_id["R1"].before is not None and by_id["R1"].before["text"] == "Original text"
    assert by_id["R1"].after is not None and by_id["R1"].after["text"] == "Changed text"


def test_diff_manifest_detects_constraints_added_removed() -> None:
    old = _manifest(constraints=["A", "B"])
    new = _manifest(constraints=["B", "C"])
    diff = diff_manifest(old, new)
    assert diff.constraints_added == ["C"]
    assert diff.constraints_removed == ["A"]
