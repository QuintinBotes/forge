"""Spec version diffing (ss-versioning): markdown + structured manifest diffs.

Pure functions over two spec snapshots — no filesystem, no engine, no DB — so
they are usable both by the API layer (diffing two persisted
``forge_db.models.SpecVersion`` rows) and directly in tests.

Two complementary views:

* :func:`diff_markdown` — a line-level unified diff of two ``spec.md`` texts
  (equal/insert/delete runs), the "what changed in prose" view.
* :func:`diff_manifest` — a structured diff of two :class:`SpecManifest`
  snapshots: scalar field changes (name/status/...), plus id-keyed adds/
  removes/modifications for each list field (requirements, acceptance
  criteria, open questions, decisions) and a plain added/removed set for the
  id-less ``constraints`` string list.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_contracts import SpecManifest

TextDiffOp = Literal["equal", "insert", "delete"]
ListChangeKind = Literal["added", "removed", "modified"]


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class TextDiffLine(_Model):
    """One line of a unified line-diff, tagged with how it changed."""

    op: TextDiffOp
    text: str


class ListItemChange(_Model):
    """One id-keyed add/remove/modify entry within a manifest list field."""

    id: str
    change: ListChangeKind
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class ScalarFieldChange(_Model):
    """A changed top-level scalar field (e.g. ``name``, ``status``)."""

    field: str
    before: Any
    after: Any


class ManifestDiff(_Model):
    """The structured diff between two :class:`SpecManifest` snapshots."""

    scalar_changes: list[ScalarFieldChange] = Field(default_factory=list)
    requirements: list[ListItemChange] = Field(default_factory=list)
    acceptance_criteria: list[ListItemChange] = Field(default_factory=list)
    open_questions: list[ListItemChange] = Field(default_factory=list)
    decisions: list[ListItemChange] = Field(default_factory=list)
    constraints_added: list[str] = Field(default_factory=list)
    constraints_removed: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Whether *anything* differs between the two snapshots."""
        return bool(
            self.scalar_changes
            or self.requirements
            or self.acceptance_criteria
            or self.open_questions
            or self.decisions
            or self.constraints_added
            or self.constraints_removed
        )


#: Top-level scalar fields compared verbatim (order = display order).
_SCALAR_FIELDS: tuple[str, ...] = ("name", "status")


def diff_markdown(old_text: str, new_text: str) -> list[TextDiffLine]:
    """Line-level diff of two ``spec.md`` texts (equal/insert/delete runs)."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    lines: list[TextDiffLine] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            lines.extend(TextDiffLine(op="equal", text=text) for text in old_lines[i1:i2])
        elif tag == "delete":
            lines.extend(TextDiffLine(op="delete", text=text) for text in old_lines[i1:i2])
        elif tag == "insert":
            lines.extend(TextDiffLine(op="insert", text=text) for text in new_lines[j1:j2])
        elif tag == "replace":
            lines.extend(TextDiffLine(op="delete", text=text) for text in old_lines[i1:i2])
            lines.extend(TextDiffLine(op="insert", text=text) for text in new_lines[j1:j2])
    return lines


def _diff_ids(old_items: list[Any], new_items: list[Any]) -> list[ListItemChange]:
    old_by_id = {item.id: item.model_dump(mode="json") for item in old_items}
    new_by_id = {item.id: item.model_dump(mode="json") for item in new_items}
    changes: list[ListItemChange] = []
    for item_id in old_by_id:
        if item_id not in new_by_id:
            changes.append(ListItemChange(id=item_id, change="removed", before=old_by_id[item_id]))
        elif old_by_id[item_id] != new_by_id[item_id]:
            changes.append(
                ListItemChange(
                    id=item_id,
                    change="modified",
                    before=old_by_id[item_id],
                    after=new_by_id[item_id],
                )
            )
    for item_id in new_by_id:
        if item_id not in old_by_id:
            changes.append(ListItemChange(id=item_id, change="added", after=new_by_id[item_id]))
    return changes


def diff_manifest(old: SpecManifest, new: SpecManifest) -> ManifestDiff:
    """Structured diff between two spec manifests (see :class:`ManifestDiff`)."""
    scalar_changes = [
        ScalarFieldChange(field=field, before=getattr(old, field), after=getattr(new, field))
        for field in _SCALAR_FIELDS
        if getattr(old, field) != getattr(new, field)
    ]
    old_constraints, new_constraints = set(old.constraints), set(new.constraints)
    return ManifestDiff(
        scalar_changes=scalar_changes,
        requirements=_diff_ids(old.requirements, new.requirements),
        acceptance_criteria=_diff_ids(old.acceptance_criteria, new.acceptance_criteria),
        open_questions=_diff_ids(old.open_questions, new.open_questions),
        decisions=_diff_ids(old.decisions, new.decisions),
        constraints_added=sorted(new_constraints - old_constraints),
        constraints_removed=sorted(old_constraints - new_constraints),
    )


__all__ = [
    "ListChangeKind",
    "ListItemChange",
    "ManifestDiff",
    "ScalarFieldChange",
    "TextDiffLine",
    "TextDiffOp",
    "diff_manifest",
    "diff_markdown",
]
