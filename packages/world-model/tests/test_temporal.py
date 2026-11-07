"""Pure tests for temporal closing (spec §3.6): pure function of the set."""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime

from conftest import make_fact

from forge_world import Mood, Precision, compute_ends
from forge_world.ledger import InvalidatedReason

SUBJECT = uuid.uuid4()
OTHER = uuid.uuid4()


def _dt(year: int) -> datetime:
    return datetime(year, 1, 1, tzinfo=UTC)


def _closure_map(facts: list) -> dict[uuid.UUID, datetime | None]:
    closures, _ = compute_ends(facts)
    return {closure.fact_id: closure.new_valid_to.instant for closure in closures}


def test_later_value_closes_earlier() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    munich = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2023))
    closures, conflicts = compute_ends([berlin, munich])
    assert conflicts == []
    assert len(closures) == 1
    assert closures[0].fact_id == berlin.id
    assert closures[0].new_valid_to.instant == _dt(2023)


def test_closing_is_insertion_order_independent() -> None:
    facts = [
        make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020)),
        make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2023)),
        make_fact(subject=SUBJECT, value="Hamburg", valid_from=_dt(2025)),
    ]
    expected = _closure_map(facts)
    assert len(expected) == 2
    for permutation in itertools.permutations(facts):
        assert _closure_map(list(permutation)) == expected


def test_unknown_start_closes_nothing_and_is_reported() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    unknown = make_fact(subject=SUBJECT, value="Munich", valid_from=None)
    closures, conflicts = compute_ends([berlin, unknown])
    assert closures == []
    assert len(conflicts) == 1
    assert conflicts[0].kind == "unordered_succession"
    assert set(conflicts[0].fact_ids) == {berlin.id, unknown.id}


def test_unknown_start_same_value_is_quiet() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    also_berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=None)
    closures, conflicts = compute_ends([berlin, also_berlin])
    assert closures == []
    assert conflicts == []


def test_explicit_end_is_never_rewritten() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020), valid_to=_dt(2022))
    munich = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2023))
    closures, _ = compute_ends([berlin, munich])
    assert closures == []


def test_derived_end_is_recomputed_and_reopens() -> None:
    berlin = make_fact(
        subject=SUBJECT,
        value="Berlin",
        valid_from=_dt(2020),
        valid_to=_dt(2023),
        end_derived=True,
    )
    munich = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2024))
    assert _closure_map([berlin, munich]) == {berlin.id: _dt(2024)}
    # Successor gone: the derived end is stale and reopens.
    assert _closure_map([berlin]) == {berlin.id: None}
    # Already closed at the right instant: no diff.
    munich_2023 = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2023))
    assert _closure_map([berlin, munich_2023]) == {}


def test_same_start_different_values_not_closed() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    munich = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2020))
    closures, _ = compute_ends([berlin, munich])
    assert closures == []  # overlapping values are conflicts, never closures


def test_mood_and_invalidated_facts_never_participate() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    planned = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2023), mood=Mood.PLAN)
    dead = make_fact(
        subject=SUBJECT,
        value="Hamburg",
        valid_from=_dt(2024),
        invalidated_at=_dt(2026),
        invalidated_reason=InvalidatedReason.RETRACTED,
    )
    closures, conflicts = compute_ends([berlin, planned, dead])
    assert closures == []
    assert conflicts == []


def test_other_subjects_and_predicates_are_isolated() -> None:
    berlin = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    other = make_fact(subject=OTHER, value="Munich", valid_from=_dt(2023))
    works = make_fact(
        subject=SUBJECT,
        predicate="works_at",
        phrase="works at",
        value="HQ",
        valid_from=_dt(2023),
    )
    closures, _ = compute_ends([berlin, other, works])
    assert closures == []


def test_same_value_rerun_closes_at_next_different_value() -> None:
    first = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    again = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2021))
    munich = make_fact(subject=SUBJECT, value="Munich", valid_from=_dt(2023))
    result = _closure_map([first, again, munich])
    assert result == {first.id: _dt(2023), again.id: _dt(2023)}


def test_both_sides_close_at_earliest_successor_any_order() -> None:
    # A is closed on the subject side by B (2022) and on the object side
    # by C (2021); every arrival order must end A at 2021 (M8).
    s1, s2, o1, o2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a = make_fact(subject=s1, object_id=o1, value=None, valid_from=_dt(2020))
    b = make_fact(subject=s1, object_id=o2, value=None, valid_from=_dt(2022))
    c = make_fact(subject=s2, object_id=o1, value=None, valid_from=_dt(2021))
    for permutation in itertools.permutations([a, b, c]):
        closures, _ = compute_ends(list(permutation), sides=["subject", "object"])
        by_id = {closure.fact_id: closure.new_valid_to for closure in closures}
        assert by_id[a.id].instant == _dt(2021)


def test_derived_end_precision_is_finest_successor_any_order() -> None:
    # B ("2022", year) and C ("2022-01-01", day) share a start instant;
    # A's derived end must take the finest precision in any order (minor 6).
    a = make_fact(subject=SUBJECT, value="Berlin", valid_from=_dt(2020))
    b = make_fact(
        subject=SUBJECT,
        value="Munich",
        valid_from=_dt(2022),
        valid_from_precision=Precision.YEAR,
    )
    c = make_fact(
        subject=SUBJECT,
        value="Hamburg",
        valid_from=_dt(2022),
        valid_from_precision=Precision.DAY,
    )
    for permutation in itertools.permutations([a, b, c]):
        closures, _ = compute_ends(list(permutation))
        by_id = {closure.fact_id: closure.new_valid_to for closure in closures}
        assert by_id[a.id].instant == _dt(2022)
        assert by_id[a.id].precision is Precision.DAY
    # An end already derived at that instant but coarser precision is
    # recomputed, not skipped.
    closed = make_fact(
        subject=SUBJECT,
        value="Berlin",
        valid_from=_dt(2020),
        valid_to=_dt(2022),
        valid_to_precision=Precision.YEAR,
        end_derived=True,
    )
    closures, _ = compute_ends([closed, b, c])
    assert len(closures) == 1
    assert closures[0].new_valid_to.precision is Precision.DAY


def test_object_side_mirrors_for_inverse_functional() -> None:
    a = make_fact(subject=SUBJECT, object_id=OTHER, value=None, valid_from=_dt(2020))
    third = uuid.uuid4()
    b = make_fact(subject=third, object_id=OTHER, value=None, valid_from=_dt(2023))
    closures, _ = compute_ends([a, b], side="object")
    assert len(closures) == 1
    assert closures[0].fact_id == a.id
    assert closures[0].new_valid_to.instant == _dt(2023)
