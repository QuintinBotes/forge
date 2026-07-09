"""Acceptance-criterion *style* tests (ss-criteria).

Criteria may be authored in three first-class styles — ``gherkin``,
``assertion`` and ``checklist`` — all encoded in the single ``text`` field so the
canonical :class:`SpecManifest` and its ``req_refs`` (R#) linking are untouched.
These tests pin the style classifier + (de)serialisers and prove that a
multi-line **checklist** criterion round-trips through ``spec.md`` *with its
requirement links intact*.
"""

from __future__ import annotations

import pytest

from forge_contracts import AcceptanceCriterion, Requirement, SpecManifest
from forge_spec import (
    ASSERTION,
    CHECKLIST,
    GHERKIN,
    ChecklistItem,
    GivenWhenThen,
    classify_criterion,
    compose_checklist,
    compose_gherkin,
    parse_checklist,
    parse_gherkin,
    parse_spec_md,
    render_spec_md,
)

# --------------------------------------------------------------------------- #
# classify_criterion                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "style"),
    [
        ("", GHERKIN),
        ("   ", GHERKIN),
        ("Given a user When they sign in Then they land on the board", GHERKIN),
        ("Then it works", GHERKIN),
        ("The endpoint returns 200 for a valid token", ASSERTION),
        ("- [ ] Email field validates\n- [x] Password is masked", CHECKLIST),
        ("- [ ] single unchecked item", CHECKLIST),
    ],
)
def test_classify_criterion(text: str, style: str) -> None:
    assert classify_criterion(text) == style


def test_checklist_wins_over_gherkin_keywords_in_labels() -> None:
    # A checklist item whose label contains "when" is still a checklist.
    text = "- [ ] Logs an event when the job runs\n- [ ] Retries on failure"
    assert classify_criterion(text) == CHECKLIST


# --------------------------------------------------------------------------- #
# checklist (de)serialisation                                                 #
# --------------------------------------------------------------------------- #


def test_checklist_round_trips() -> None:
    items = [
        ChecklistItem(label="Email field validates", checked=False),
        ChecklistItem(label="Password is masked", checked=True),
    ]
    text = compose_checklist(items)
    assert text == "- [ ] Email field validates\n- [x] Password is masked"
    assert parse_checklist(text) == items


def test_checklist_empty_label_has_no_trailing_space() -> None:
    assert compose_checklist([ChecklistItem(label="", checked=False)]) == "- [ ]"


def test_parse_checklist_tolerates_non_item_lines() -> None:
    assert parse_checklist("plain line") == [ChecklistItem(label="plain line", checked=False)]


# --------------------------------------------------------------------------- #
# gherkin (de)serialisation                                                   #
# --------------------------------------------------------------------------- #


def test_gherkin_round_trips() -> None:
    parts = GivenWhenThen(given="a user", when="they sign in", then="they land on the board")
    text = compose_gherkin(parts)
    assert text == "Given a user When they sign in Then they land on the board"
    assert parse_gherkin(text) == parts


def test_gherkin_unstructured_text_becomes_then_clause() -> None:
    assert parse_gherkin("just some prose") == GivenWhenThen(
        given="", when="", then="just some prose"
    )


# --------------------------------------------------------------------------- #
# spec.md round-trip for multi-line / checklist criteria (R# links intact)    #
# --------------------------------------------------------------------------- #


def _mixed_style_manifest() -> SpecManifest:
    return SpecManifest(
        id="SPEC-7",
        name="Login flow",
        requirements=[
            Requirement(id="R1", text="Users can sign in"),
            Requirement(id="R2", text="Sign-in form is accessible"),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="A1",
                req_refs=["R1"],
                text="Given valid credentials When submitted Then the board loads",
            ),
            AcceptanceCriterion(
                id="A2",
                req_refs=["R1"],
                text="The API returns 200 for a valid session token",
            ),
            AcceptanceCriterion(
                id="A3",
                req_refs=["R2"],
                text=(
                    "- [ ] Email field validates\n"
                    "- [x] Password is masked\n"
                    "- [ ] Submit disabled when empty"
                ),
                spec_ref="SPEC-2",
            ),
        ],
    )


def test_mixed_style_manifest_round_trips() -> None:
    manifest = _mixed_style_manifest()
    assert parse_spec_md(render_spec_md(manifest)) == manifest


def test_render_is_stable_for_mixed_styles() -> None:
    rendered = render_spec_md(_mixed_style_manifest())
    assert render_spec_md(parse_spec_md(rendered)) == rendered


def test_checklist_criterion_renders_as_continuation_lines() -> None:
    rendered = render_spec_md(_mixed_style_manifest())
    # Header carries id + refs + spec_ref + first item; rest are 2-space bullets.
    assert "- **A3** (R2; spec=SPEC-2): - [ ] Email field validates" in rendered
    assert "\n  - [x] Password is masked\n" in rendered


def test_checklist_criterion_keeps_req_refs_after_round_trip() -> None:
    parsed = parse_spec_md(render_spec_md(_mixed_style_manifest()))
    a3 = parsed.acceptance_criteria[2]
    assert a3.req_refs == ["R2"]
    assert a3.spec_ref == "SPEC-2"
    assert classify_criterion(a3.text) == CHECKLIST
    assert parse_checklist(a3.text) == [
        ChecklistItem(label="Email field validates", checked=False),
        ChecklistItem(label="Password is masked", checked=True),
        ChecklistItem(label="Submit disabled when empty", checked=False),
    ]


def test_dangling_acceptance_continuation_raises() -> None:
    from forge_spec import SpecParseError

    text = (
        "---\nid: SPEC-1\n---\n\n## Goal\n\nName\n\n"
        "## Acceptance Criteria\n\n  - [ ] orph continuation\n"
    )
    with pytest.raises(SpecParseError):
        parse_spec_md(text)
