"""Acceptance-criterion *styles* for the spec engine (ss-criteria).

An :class:`~forge_contracts.AcceptanceCriterion` carries free-form ``text`` plus
its requirement links (``req_refs``). Historically that text was written in one
shape — Given/When/Then. This module lets a criterion be written in any of three
first-class *styles*, all encoded losslessly inside the same ``text`` field so
the canonical :class:`~forge_contracts.SpecManifest` and its ``req_refs`` linking
are untouched:

- ``gherkin``    — ``Given … When … Then …`` behavioural prose (the default).
- ``assertion``  — a single plain declarative sentence.
- ``checklist``  — one or more ``- [ ] item`` / ``- [x] item`` lines (multi-line
  ``text``; round-trips through ``spec.md`` via continuation lines — see
  :mod:`forge_spec.markdown`).

:func:`classify_criterion` infers a criterion's style from its text (best-effort,
never raising) so guided editors, renderers and dashboards can present the right
affordance. :func:`parse_checklist` / :func:`compose_checklist` and
:func:`parse_gherkin` / :func:`compose_gherkin` (de)serialise the two structured
styles. Style is *derived*, not stored: nothing here changes the frozen
``AcceptanceCriterion`` contract, and requirement (R#) linking is never touched.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

#: The three acceptance-criterion authoring styles.
CriterionStyle = Literal["gherkin", "assertion", "checklist"]

GHERKIN: CriterionStyle = "gherkin"
ASSERTION: CriterionStyle = "assertion"
CHECKLIST: CriterionStyle = "checklist"

#: ``- [ ] label`` / ``- [x] label`` (the checked box is case-insensitive; the
#: space after ``]`` is optional so hand-authored items still classify).
_CHECK_ITEM = re.compile(r"^- \[(?P<mark>[ xX])\] ?(?P<label>.*)$")
#: Gherkin structural keywords — their presence marks behavioural prose.
_GHERKIN_KEYWORD = re.compile(r"\b(?:given|when|then)\b", re.IGNORECASE)
#: Independent Given/When/Then clause extractors (partial edits round-trip).
_GIVEN = re.compile(r"Given\s+(.*?)(?=\s+When\s+|\s+Then\s+|$)", re.IGNORECASE | re.DOTALL)
_WHEN = re.compile(r"When\s+(.*?)(?=\s+Then\s+|$)", re.IGNORECASE | re.DOTALL)
_THEN = re.compile(r"Then\s+(.*)$", re.IGNORECASE | re.DOTALL)


class ChecklistItem(NamedTuple):
    """One checklist entry: a ``label`` and whether its box is ``checked``."""

    label: str
    checked: bool


class GivenWhenThen(NamedTuple):
    """The three clauses of a gherkin criterion (any may be empty)."""

    given: str
    when: str
    then: str


def _nonblank_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def classify_criterion(text: str) -> CriterionStyle:
    """Infer the authoring style of a criterion's ``text`` (never raises).

    Empty/blank text defaults to :data:`GHERKIN` (the editor's default shape).
    Checklist wins over gherkin when every non-blank line is a check item, so a
    checklist whose labels happen to contain ``when`` is still a checklist.
    """
    lines = _nonblank_lines(text)
    if not lines:
        return GHERKIN
    if all(_CHECK_ITEM.match(line) for line in lines):
        return CHECKLIST
    if _GHERKIN_KEYWORD.search(text):
        return GHERKIN
    return ASSERTION


def parse_checklist(text: str) -> list[ChecklistItem]:
    """Parse checklist ``text`` into items (non-item lines become unchecked)."""
    items: list[ChecklistItem] = []
    for line in _nonblank_lines(text):
        match = _CHECK_ITEM.match(line)
        if match is None:
            items.append(ChecklistItem(label=line, checked=False))
            continue
        items.append(
            ChecklistItem(label=match["label"].strip(), checked=match["mark"] in ("x", "X"))
        )
    return items


def compose_checklist(items: list[ChecklistItem]) -> str:
    """Render checklist ``items`` back to canonical ``- [ ] label`` lines."""
    return "\n".join(f"- [{'x' if item.checked else ' '}] {item.label}".rstrip() for item in items)


def parse_gherkin(text: str) -> GivenWhenThen:
    """Best-effort split of ``text`` into Given/When/Then clauses.

    When no keyword is present the whole text is treated as the ``then`` clause
    (mirrors the guided editor's ``parseGivenWhenThen``).
    """
    trimmed = text.strip()
    given = _GIVEN.search(trimmed)
    when = _WHEN.search(trimmed)
    then = _THEN.search(trimmed)
    if given is None and when is None and then is None:
        return GivenWhenThen(given="", when="", then=trimmed)
    return GivenWhenThen(
        given=given.group(1).strip() if given else "",
        when=when.group(1).strip() if when else "",
        then=then.group(1).strip() if then else "",
    )


def compose_gherkin(parts: GivenWhenThen) -> str:
    """Compose Given/When/Then ``parts`` back into a single criterion text."""
    chunks: list[str] = []
    if parts.given:
        chunks.append(f"Given {parts.given}")
    if parts.when:
        chunks.append(f"When {parts.when}")
    if parts.then:
        chunks.append(f"Then {parts.then}")
    return " ".join(chunks)


__all__ = [
    "ASSERTION",
    "CHECKLIST",
    "GHERKIN",
    "ChecklistItem",
    "CriterionStyle",
    "GivenWhenThen",
    "classify_criterion",
    "compose_checklist",
    "compose_gherkin",
    "parse_checklist",
    "parse_gherkin",
]
