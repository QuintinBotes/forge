"""Pure tests for reasoning: each axiom, limits, asserted-beats-derived."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from conftest import make_fact

from forge_world import Layer, Mood, RelationDef, RelationKind, derive

A = uuid.uuid4()
B = uuid.uuid4()
C = uuid.uuid4()
D = uuid.uuid4()


def _dt(year: int) -> datetime:
    return datetime(year, 1, 1, tzinfo=UTC)


def _rel(key: str, **flags: object) -> RelationDef:
    return RelationDef(key=key, label=key, kind=RelationKind.STATE, **flags)  # type: ignore[arg-type]


def _triples(derived: list) -> set[tuple[uuid.UUID, str, uuid.UUID | None, str | None]]:
    return {(d.subject_id, d.predicate_key, d.object_id, d.value) for d in derived}


def test_symmetric() -> None:
    fact = make_fact(subject=A, predicate="partner_of", object_id=B, value=None)
    derived, violations, capped = derive([fact], [_rel("partner_of", symmetric=True)])
    assert violations == [] and capped == []
    assert _triples(derived) == {(B, "partner_of", A, None)}
    assert derived[0].rule == "symmetric"
    assert derived[0].premise_ids == (fact.id,)


def test_inverse() -> None:
    fact = make_fact(subject=A, predicate="parent_of", object_id=B, value=None)
    relations = [
        _rel("parent_of", inverse_of="child_of"),
        _rel("child_of", inverse_of="parent_of"),
    ]
    derived, _, _ = derive([fact], relations)
    assert (B, "child_of", A, None) in _triples(derived)
    rules = {item.rule for item in derived}
    assert rules == {"inverse"}


def test_sub_property() -> None:
    fact = make_fact(subject=A, predicate="ceo_of", object_id=B, value=None)
    relations = [_rel("ceo_of", sub_property_of="works_at"), _rel("works_at")]
    derived, _, _ = derive([fact], relations)
    assert _triples(derived) == {(A, "works_at", B, None)}


def test_sub_property_applies_to_values() -> None:
    fact = make_fact(subject=A, predicate="hq_city", value="Berlin")
    relations = [_rel("hq_city", sub_property_of="located_in"), _rel("located_in")]
    derived, _, _ = derive([fact], relations)
    assert _triples(derived) == {(A, "located_in", None, "Berlin")}


def test_transitive_chain() -> None:
    ab = make_fact(subject=A, predicate="part_of", object_id=B, value=None)
    bc = make_fact(subject=B, predicate="part_of", object_id=C, value=None)
    cd = make_fact(subject=C, predicate="part_of", object_id=D, value=None)
    derived, _, _ = derive([ab, bc, cd], [_rel("part_of", transitive=True)])
    triples = _triples(derived)
    assert (A, "part_of", C, None) in triples
    assert (B, "part_of", D, None) in triples
    assert (A, "part_of", D, None) in triples  # through a derived premise
    deep = next(d for d in derived if d.subject_id == A and d.object_id == D)
    assert deep.depth == 2
    assert set(deep.premise_ids)  # proof carried


def test_validity_is_interval_intersection() -> None:
    ab = make_fact(
        subject=A,
        predicate="part_of",
        object_id=B,
        value=None,
        valid_from=_dt(2020),
        valid_to=_dt(2025),
        confidence=0.9,
    )
    bc = make_fact(
        subject=B,
        predicate="part_of",
        object_id=C,
        value=None,
        valid_from=_dt(2022),
        valid_to=None,
        confidence=0.7,
    )
    derived, _, _ = derive([ab, bc], [_rel("part_of", transitive=True)])
    assert len(derived) == 1
    assert derived[0].valid_from.instant == _dt(2022)
    assert derived[0].valid_to.instant == _dt(2025)
    assert derived[0].confidence == 0.7  # minimum of premises


def test_empty_intersection_means_no_derivation() -> None:
    ab = make_fact(
        subject=A,
        predicate="part_of",
        object_id=B,
        value=None,
        valid_from=_dt(2020),
        valid_to=_dt(2021),
    )
    bc = make_fact(
        subject=B,
        predicate="part_of",
        object_id=C,
        value=None,
        valid_from=_dt(2022),
        valid_to=None,
    )
    derived, _, _ = derive([ab, bc], [_rel("part_of", transitive=True)])
    assert derived == []


def test_asserted_identical_is_not_derived() -> None:
    ab = make_fact(subject=A, predicate="partner_of", object_id=B, value=None)
    ba = make_fact(subject=B, predicate="partner_of", object_id=A, value=None)
    derived, _, _ = derive([ab, ba], [_rel("partner_of", symmetric=True)])
    assert derived == []  # both directions already asserted


def test_derived_contradicting_asserted_functional_is_a_violation() -> None:
    # A reports to B; B is a supervisor; but A is asserted to report to C.
    asserted = make_fact(
        subject=A,
        predicate="reports_to",
        object_id=C,
        value=None,
        valid_from=_dt(2020),
    )
    premise = make_fact(
        subject=A,
        predicate="works_for",
        object_id=B,
        value=None,
        valid_from=_dt(2021),
    )
    relations = [
        _rel("reports_to", functional=True),
        _rel("works_for", sub_property_of="reports_to"),
    ]
    derived, violations, _ = derive([asserted, premise], relations)
    assert derived == []
    assert len(violations) == 1
    assert violations[0].asserted_fact_id == asserted.id
    assert violations[0].object_id == B


def test_depth_cap() -> None:
    chain = [uuid.uuid4() for _ in range(16)]
    facts = [
        make_fact(subject=chain[i], predicate="p", object_id=chain[i + 1], value=None)
        for i in range(len(chain) - 1)
    ]
    derived, _, _ = derive(facts, [_rel("p", transitive=True)], max_depth=3)
    assert derived
    assert max(item.depth for item in derived) == 3


def test_per_predicate_cap() -> None:
    ab = make_fact(subject=A, predicate="p", object_id=B, value=None)
    bc = make_fact(subject=B, predicate="p", object_id=C, value=None)
    cd = make_fact(subject=C, predicate="p", object_id=D, value=None)
    derived, _, capped = derive([ab, bc, cd], [_rel("p", transitive=True)], max_per_predicate=1)
    assert len(derived) == 1
    assert capped == ["p"]


def test_mood_and_untyped_facts_never_participate() -> None:
    planned = make_fact(subject=A, predicate="partner_of", object_id=B, value=None, mood=Mood.PLAN)
    open_fact = make_fact(
        subject=A,
        predicate=None,
        phrase="partners with",
        object_id=B,
        value=None,
        layer=Layer.OPEN,
    )
    derived, _, _ = derive([planned, open_fact], [_rel("partner_of", symmetric=True)])
    assert derived == []


def test_frontier_chain_derives_every_reachable_pair() -> None:
    # The frontier must still reach the same fixed point: every pair of
    # the chain, in chain order, is derived exactly once (minor 13).
    chain = [uuid.uuid4() for _ in range(7)]
    facts = [
        make_fact(subject=chain[i], predicate="p", object_id=chain[i + 1], value=None)
        for i in range(len(chain) - 1)
    ]
    derived, violations, capped = derive(facts, [_rel("p", transitive=True)])
    assert violations == [] and capped == []
    expected = {
        (chain[i], "p", chain[j], None) for i in range(len(chain)) for j in range(i + 2, len(chain))
    }
    assert _triples(derived) == expected
    assert len(derived) == len(expected)


def test_cycles_terminate() -> None:
    ab = make_fact(subject=A, predicate="p", object_id=B, value=None)
    ba = make_fact(subject=B, predicate="p", object_id=A, value=None)
    derived, _, _ = derive([ab, ba], [_rel("p", transitive=True, symmetric=True)])
    # Self-consequences from the cycle are entailed; the fixed point ends.
    assert all(item.depth <= 12 for item in derived)
