"""``spec.md`` dual-format (de)serialization tests for ``forge_spec`` (ss-parser).

``spec.md`` (prose) and ``manifest.yaml`` (machine) are BOTH canonical, non-lossy
serializations of the one :class:`SpecManifest`. These tests pin:

- ``parse_spec_md`` is the exact inverse of ``render_spec_md``
  (``parse(render(m)) == m`` and ``render(parse(render(m))) == render(m)``),
- messy input raises a line-anchored :class:`SpecParseError`,
- the YAML path (``load_manifest``/``dump_manifest``) round-trips, and
- a spec.md and its manifest.yaml parse to the *same* ``SpecManifest``
  (cross-format consistency — neither format is lossy).
"""

from __future__ import annotations

import pytest

from forge_contracts import (
    ADR,
    AcceptanceCriterion,
    ExecutionMode,
    OpenQuestion,
    Requirement,
    SpecManifest,
    SpecStatus,
)
from forge_spec import (
    SpecParseError,
    dump_manifest,
    load_manifest,
    parse_spec_md,
    render_spec_md,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _full_manifest() -> SpecManifest:
    return SpecManifest(
        id="SPEC-17",
        name="Customer endpoint improvements",
        status=SpecStatus.APPROVED,
        constitution_refs=["engineering/api-principles", "security/auth"],
        repos=["github.com/org/api"],
        requirements=[
            Requirement(id="R1", text="Add customer search endpoint"),
            Requirement(id="R2", text="Endpoint must support bearer auth"),
        ],
        acceptance_criteria=[
            AcceptanceCriterion(id="A1", req_refs=["R1"], text="cursor + limit params"),
            AcceptanceCriterion(
                id="A2",
                req_refs=["R1", "R2"],
                text="Given no bearer, When called, Then 401",
                spec_ref="SPEC-9",
            ),
        ],
        constraints=["No breaking changes before v2", "P99 < 200ms"],
        open_questions=[
            OpenQuestion(id="Q1", text="Rate limit policy?"),
            OpenQuestion(id="Q2", text="Which regions?", resolution="us-east only"),
        ],
        decisions=[
            ADR(
                id="ADR-1",
                title="Use cursor pagination",
                status="accepted",
                context="Offset pagination is unstable under writes.",
                decision="Adopt opaque cursor tokens.",
                consequences="Clients cannot random-access pages.",
            ),
            ADR(id="ADR-2", title="Bare decision"),
        ],
        plan_ref="plan.md",
        tasks_ref="tasks.md",
        validation_ref="validation.md",
        execution_mode=ExecutionMode.SUPERVISED_MULTI_AGENT,
        skill_profile="backend-tdd",
    )


def _minimal_manifest() -> SpecManifest:
    return SpecManifest(id="SPEC-1", name="Tiny spec")


# --------------------------------------------------------------------------- #
# Round-trip: parse(render(m)) == m                                            #
# --------------------------------------------------------------------------- #


def test_render_parse_round_trips_full_manifest() -> None:
    manifest = _full_manifest()
    assert parse_spec_md(render_spec_md(manifest)) == manifest


def test_render_parse_round_trips_minimal_manifest() -> None:
    manifest = _minimal_manifest()
    assert parse_spec_md(render_spec_md(manifest)) == manifest


def test_render_is_stable_under_parse_render() -> None:
    rendered = render_spec_md(_full_manifest())
    assert render_spec_md(parse_spec_md(rendered)) == rendered


def test_status_and_execution_mode_survive_round_trip() -> None:
    manifest = _full_manifest()
    parsed = parse_spec_md(render_spec_md(manifest))
    assert parsed.status is SpecStatus.APPROVED
    assert parsed.execution_mode is ExecutionMode.SUPERVISED_MULTI_AGENT


def test_acceptance_req_refs_and_spec_ref_survive_round_trip() -> None:
    parsed = parse_spec_md(render_spec_md(_full_manifest()))
    a2 = parsed.acceptance_criteria[1]
    assert a2.req_refs == ["R1", "R2"]
    assert a2.spec_ref == "SPEC-9"


def test_open_question_resolution_optional_round_trip() -> None:
    parsed = parse_spec_md(render_spec_md(_full_manifest()))
    assert parsed.open_questions[0].resolution is None
    assert parsed.open_questions[1].resolution == "us-east only"


def test_bare_and_full_adr_round_trip() -> None:
    parsed = parse_spec_md(render_spec_md(_full_manifest()))
    bare = parsed.decisions[1]
    assert bare.id == "ADR-2"
    assert bare.status == "proposed"  # the model default
    assert bare.context is None and bare.decision is None and bare.consequences is None


def test_unicode_survives_round_trip() -> None:
    manifest = SpecManifest(
        id="SPEC-42",
        name="Über café — naïve façade",
        requirements=[Requirement(id="R1", text="Support Ünïcödé — 日本語 ✓")],
    )
    assert parse_spec_md(render_spec_md(manifest)) == manifest


def test_goal_section_carries_the_name() -> None:
    text = render_spec_md(_full_manifest())
    assert "## Goal\n\nCustomer endpoint improvements" in text


def test_render_begins_with_frontmatter_fence() -> None:
    assert render_spec_md(_minimal_manifest()).startswith("---\n")


# --------------------------------------------------------------------------- #
# YAML path + cross-format consistency                                        #
# --------------------------------------------------------------------------- #


def test_manifest_yaml_round_trips() -> None:
    manifest = _full_manifest()
    assert load_manifest(dump_manifest(manifest)) == manifest


def test_spec_md_and_manifest_yaml_agree() -> None:
    """spec.md and manifest.yaml for the SAME spec parse to the SAME manifest."""
    manifest = _full_manifest()
    from_md = parse_spec_md(render_spec_md(manifest))
    from_yaml = load_manifest(dump_manifest(manifest))
    assert from_md == from_yaml == manifest


def test_cross_format_consistency_minimal() -> None:
    manifest = _minimal_manifest()
    assert parse_spec_md(render_spec_md(manifest)) == load_manifest(dump_manifest(manifest))


# --------------------------------------------------------------------------- #
# Messy input: line-anchored SpecParseError                                    #
# --------------------------------------------------------------------------- #


def test_missing_frontmatter_raises_at_line_one() -> None:
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md("## Goal\n\nNo frontmatter here\n")
    assert exc.value.line == 1


def test_unterminated_frontmatter_raises() -> None:
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md("---\nid: SPEC-1\n\n## Goal\n\nX\n")
    assert "unterminated" in exc.value.raw_message.lower()


def test_frontmatter_not_a_mapping_raises() -> None:
    with pytest.raises(SpecParseError):
        parse_spec_md("---\n- just\n- a\n- list\n---\n\n## Goal\n\nX\n")


def test_missing_id_raises() -> None:
    text = "---\nstatus: draft\n---\n\n## Goal\n\nNo id given\n"
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert "id" in exc.value.raw_message


def test_missing_goal_section_raises() -> None:
    text = "---\nid: SPEC-1\n---\n\n## Requirements\n\n- **R1**: x\n"
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert "Goal" in exc.value.raw_message


def test_empty_goal_section_raises() -> None:
    with pytest.raises(SpecParseError):
        parse_spec_md("---\nid: SPEC-1\n---\n\n## Goal\n\n")


def test_malformed_requirement_bullet_is_line_anchored() -> None:
    text = (
        "---\nid: SPEC-1\n---\n\n"  # lines 1(---) 2(id) 3(---) 4(blank)
        "## Goal\n\nName\n\n"  # lines 5-8
        "## Requirements\n\n"  # lines 9-10
        "- R1 missing the bold marker\n"  # line 11
    )
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert exc.value.line == 11


def test_malformed_acceptance_bullet_raises() -> None:
    text = (
        "---\nid: SPEC-1\n---\n\n## Goal\n\nName\n\n"
        "## Acceptance Criteria\n\n- plain text with no id\n"
    )
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert "acceptance" in exc.value.raw_message.lower()


def test_unknown_section_raises_at_header_line() -> None:
    text = "---\nid: SPEC-1\n---\n\n## Goal\n\nName\n\n## Nonsense\n\n- x\n"
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert exc.value.line == 9
    assert "Nonsense" in exc.value.raw_message


def test_content_before_first_section_raises() -> None:
    text = "---\nid: SPEC-1\n---\n\nstray prose\n\n## Goal\n\nName\n"
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert exc.value.line == 5


def test_malformed_decision_heading_raises() -> None:
    text = "---\nid: SPEC-1\n---\n\n## Goal\n\nName\n\n## Decisions\n\n### ADR-1 no separator\n"
    with pytest.raises(SpecParseError) as exc:
        parse_spec_md(text)
    assert "###" in exc.value.raw_message or "heading" in exc.value.raw_message.lower()


def test_dangling_resolution_raises() -> None:
    text = (
        "---\nid: SPEC-1\n---\n\n## Goal\n\nName\n\n## Open Questions\n\n  - Resolution: orphan\n"
    )
    with pytest.raises(SpecParseError):
        parse_spec_md(text)


def test_spec_parse_error_str_includes_line() -> None:
    err = SpecParseError("boom", line=7)
    assert str(err) == "line 7: boom"
    assert err.line == 7


def test_spec_parse_error_is_forge_error_and_value_error() -> None:
    from forge_contracts import ForgeError

    err = SpecParseError("boom")
    assert isinstance(err, ForgeError)
    assert isinstance(err, ValueError)
    assert err.line is None
