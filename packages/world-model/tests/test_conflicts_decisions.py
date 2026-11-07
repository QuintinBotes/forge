"""Pure tests for functional conflicts and decision drift."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from conftest import make_fact

from forge_world import (
    InvalidatedReason,
    RelationDef,
    RelationKind,
    drift,
    functional_conflicts,
)

A = uuid.uuid4()
B = uuid.uuid4()
C = uuid.uuid4()

BASED_IN = RelationDef(
    key="based_in",
    label="based in",
    kind=RelationKind.STATE,
    range_class="value",
    functional=True,
)
HAS_CEO = RelationDef(
    key="has_ceo", label="has CEO", kind=RelationKind.STATE, inverse_functional=True
)


def _dt(year: int) -> datetime:
    return datetime(year, 1, 1, tzinfo=UTC)


def test_overlapping_functional_values_conflict() -> None:
    berlin = make_fact(subject=A, value="Berlin", valid_from=_dt(2020), valid_to=_dt(2025))
    munich = make_fact(subject=A, value="Munich", valid_from=_dt(2023), valid_to=None)
    conflicts = functional_conflicts([berlin, munich], [BASED_IN])
    assert len(conflicts) == 1
    assert conflicts[0].kind == "functional_overlap"
    assert set(conflicts[0].fact_ids) == {berlin.id, munich.id}


def test_clean_succession_is_not_a_conflict() -> None:
    berlin = make_fact(subject=A, value="Berlin", valid_from=_dt(2020), valid_to=_dt(2023))
    munich = make_fact(subject=A, value="Munich", valid_from=_dt(2023), valid_to=None)
    assert functional_conflicts([berlin, munich], [BASED_IN]) == []


def test_same_value_overlap_is_not_a_conflict() -> None:
    one = make_fact(subject=A, value="Berlin", valid_from=_dt(2020), valid_to=_dt(2025))
    two = make_fact(subject=A, value="Berlin", valid_from=_dt(2022), valid_to=None)
    assert functional_conflicts([one, two], [BASED_IN]) == []


def test_unknown_start_pairs_are_left_to_temporal() -> None:
    berlin = make_fact(subject=A, value="Berlin", valid_from=None)
    munich = make_fact(subject=A, value="Munich", valid_from=_dt(2023))
    assert functional_conflicts([berlin, munich], [BASED_IN]) == []


def test_inverse_functional_overlap() -> None:
    one = make_fact(
        subject=A,
        predicate="has_ceo",
        phrase="has CEO",
        object_id=C,
        value=None,
        valid_from=_dt(2020),
    )
    two = make_fact(
        subject=B,
        predicate="has_ceo",
        phrase="has CEO",
        object_id=C,
        value=None,
        valid_from=_dt(2021),
    )
    conflicts = functional_conflicts([one, two], [HAS_CEO])
    assert len(conflicts) == 1
    assert conflicts[0].kind == "inverse_functional_overlap"
    assert conflicts[0].object_id == C


def test_unknown_start_eternal_pair_is_a_conflict() -> None:
    # Temporal closing only runs for STATE relations, so an eternal
    # functional pair with unknown starts must surface here (M10).
    born_in = RelationDef(
        key="born_in",
        label="born in",
        kind=RelationKind.ETERNAL,
        range_class="value",
        functional=True,
    )
    uk = make_fact(subject=A, predicate="born_in", phrase="born in", value="UK", valid_from=None)
    fr = make_fact(
        subject=A, predicate="born_in", phrase="born in", value="France", valid_from=None
    )
    conflicts = functional_conflicts([uk, fr], [born_in])
    assert len(conflicts) == 1
    assert conflicts[0].kind == "functional_overlap"


def test_unknown_start_event_pair_is_a_conflict() -> None:
    founded = RelationDef(
        key="founded_in",
        label="founded in",
        kind=RelationKind.EVENT,
        range_class="value",
        functional=True,
    )
    one = make_fact(
        subject=A, predicate="founded_in", phrase="founded in", value="UK", valid_from=None
    )
    two = make_fact(
        subject=A, predicate="founded_in", phrase="founded in", value="France", valid_from=None
    )
    conflicts = functional_conflicts([one, two], [founded])
    assert len(conflicts) == 1
    assert conflicts[0].kind == "functional_overlap"


def test_non_functional_relations_do_not_conflict() -> None:
    plain = RelationDef(key="based_in", label="based in", kind=RelationKind.STATE)
    berlin = make_fact(subject=A, value="Berlin", valid_from=_dt(2020))
    munich = make_fact(subject=A, value="Munich", valid_from=_dt(2021))
    assert functional_conflicts([berlin, munich], [plain]) == []


def test_drift_statuses() -> None:
    live = make_fact(subject=A, value="Berlin")
    retracted = make_fact(
        subject=A,
        value="Munich",
        invalidated_at=_dt(2026),
        invalidated_reason=InvalidatedReason.RETRACTED,
    )
    old = make_fact(
        subject=A,
        value="Hamburg",
        invalidated_at=_dt(2026),
        invalidated_reason=InvalidatedReason.SUPERSEDED,
    )
    successor_id = uuid.uuid4()
    report = drift([live, retracted, old], {old.id: successor_id})
    assert report.drifted is True
    by_id = {item.fact_id: item for item in report.items}
    assert by_id[live.id].status == "unchanged"
    assert by_id[retracted.id].status == "invalidated"
    assert by_id[retracted.id].reason == "retracted"
    assert by_id[old.id].status == "superseded"
    assert by_id[old.id].superseded_by == successor_id


def test_drift_clean_when_all_live() -> None:
    report = drift([make_fact(subject=A), make_fact(subject=B)])
    assert report.drifted is False
    assert all(item.status == "unchanged" for item in report.items)
