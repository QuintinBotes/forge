"""Repository + service tests on SQLite (spec §8 unit lane, §9 criteria)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from conftest import WS, WS2, FakeClock, FakeModelClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from forge_db.models.world import WorldEntityMerge, WorldFact, WorldProposal
from forge_world import (
    ConflictError,
    ForbiddenError,
    InvalidatedReason,
    Layer,
    ModelNotConfiguredError,
    NotFoundError,
    RelationKind,
    SourceKind,
    StatementInput,
    WorldModelService,
    WorldRepository,
    WorldValidationError,
)

PERSON = SourceKind.PERSON


def _dt(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


@pytest.fixture
def based_in(service: WorldModelService, session: Session) -> None:
    service.create_relation(
        WS,
        key="based_in",
        label="based in",
        kind=RelationKind.STATE,
        range_class="value",
        functional=True,
    )
    session.commit()


def _based(subject: str, value: str, *, valid_from: str | None = None) -> StatementInput:
    return StatementInput(
        phrase="based in",
        subject_name=subject,
        predicate_key="based_in",
        value=value,
        valid_from_text=valid_from,
    )


# --- dedup and basic writes (criterion 1, SQLite side) ---------------------- #


@pytest.mark.usefixtures("based_in")
def test_asserting_same_fact_twice_yields_one_live_row(
    service: WorldModelService, session: Session
) -> None:
    first = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    second = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    session.commit()
    assert first.created is True
    assert second.created is False
    assert first.fact.id == second.fact.id
    live = [
        fact for fact in service.query_facts(WS, predicate="based_in") if fact.value == "Berlin"
    ]
    assert len(live) == 1


@pytest.mark.usefixtures("based_in")
def test_reassert_after_retract_is_allowed(service: WorldModelService, session: Session) -> None:
    first = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    service.retract(WS, first.fact.id, actor="u1")
    again = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    session.commit()
    assert again.created is True
    assert again.fact.id != first.fact.id
    assert again.fact.invalidated_at is None


def test_unknown_relation_key_is_422(
    service: WorldModelService,
) -> None:
    with pytest.raises(WorldValidationError):
        service.assert_input(
            WS,
            StatementInput(
                phrase="haunts", subject_name="Acme", predicate_key="haunts", value="Berlin"
            ),
            source_kind=PERSON,
            actor="u1",
        )


@pytest.mark.usefixtures("based_in")
def test_state_span_with_reversed_ends_is_refused(
    service: WorldModelService,
) -> None:
    with pytest.raises(WorldValidationError):
        service.assert_input(
            WS,
            StatementInput(
                phrase="based in",
                subject_name="Acme",
                predicate_key="based_in",
                value="Berlin",
                valid_from_text="2024-06-01T00:00:00Z",
                valid_to_text="2023-06-01T00:00:00Z",
            ),
            source_kind=PERSON,
            actor="u1",
        )


@pytest.mark.usefixtures("based_in")
def test_state_span_with_equal_ends_is_refused(service: WorldModelService) -> None:
    # "2024-06-01" to "2024-06-01" would otherwise store a one-day span.
    with pytest.raises(WorldValidationError):
        service.assert_input(
            WS,
            StatementInput(
                phrase="based in",
                subject_name="Acme",
                predicate_key="based_in",
                value="Berlin",
                valid_from_text="2024-06-01",
                valid_to_text="2024-06-01",
            ),
            source_kind=PERSON,
            actor="u1",
        )
    with pytest.raises(WorldValidationError):
        service.assert_input(
            WS,
            StatementInput(
                phrase="based in",
                subject_name="Acme",
                predicate_key="based_in",
                value="Berlin",
                valid_from_text="2024-06-01T10:00:00Z",
                valid_to_text="2024-06-01T10:00:00Z",
            ),
            source_kind=PERSON,
            actor="u1",
        )


@pytest.mark.usefixtures("based_in")
def test_unknown_entity_id_is_not_found(service: WorldModelService) -> None:
    with pytest.raises(NotFoundError):
        service.assert_input(
            WS,
            StatementInput(
                phrase="based in",
                subject_id=uuid.uuid4(),
                predicate_key="based_in",
                value="Berlin",
            ),
            source_kind=PERSON,
            actor="u1",
        )


def test_retract_and_supersede_of_invalidated_conflict(
    service: WorldModelService, based_in: None, session: Session
) -> None:
    first = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    session.commit()
    service.retract(WS, first.fact.id, actor="u1")
    session.commit()
    with pytest.raises(ConflictError):
        service.retract(WS, first.fact.id, actor="u1")
    with pytest.raises(ConflictError):
        service.supersede(
            WS, first.fact.id, _based("Acme", "Munich"), source_kind=PERSON, actor="u1"
        )


# --- two clocks (criterion 2) ------------------------------------------------ #


@pytest.mark.usefixtures("based_in")
def test_as_of_before_correction_returns_old_value(
    service: WorldModelService, clock: FakeClock, session: Session
) -> None:
    first = service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    clock.tick(days=1)
    corrected_at = clock.now
    service.supersede(
        WS,
        first.fact.id,
        _based("Acme", "Munich", valid_from="2020"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()

    before = service.query_facts(
        WS, predicate="based_in", at=_dt(2021), as_of=first.fact.recorded_at
    )
    assert [fact.value for fact in before] == ["Berlin"]
    after = service.query_facts(WS, predicate="based_in", at=_dt(2021), as_of=corrected_at)
    assert [fact.value for fact in after] == ["Munich"]


@pytest.mark.usefixtures("based_in")
def test_at_selects_by_world_time_independently(
    service: WorldModelService, session: Session
) -> None:
    service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    service.assert_input(
        WS, _based("Acme", "Munich", valid_from="2023"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    assert [f.value for f in service.query_facts(WS, predicate="based_in", at=_dt(2021))] == [
        "Berlin"
    ]
    assert [f.value for f in service.query_facts(WS, predicate="based_in", at=_dt(2024))] == [
        "Munich"
    ]


# --- temporal closing (criterion 4) ------------------------------------------ #


@pytest.mark.parametrize("later_first", [False, True])
@pytest.mark.usefixtures("based_in")
def test_functional_state_closing_any_arrival_order(
    service: WorldModelService, session: Session, later_first: bool
) -> None:
    inputs = [
        _based("Acme", "Berlin", valid_from="2020"),
        _based("Acme", "Munich", valid_from="2023"),
    ]
    if later_first:
        inputs.reverse()
    for inp in inputs:
        service.assert_input(WS, inp, source_kind=PERSON, actor="u1")
    session.commit()

    facts = service.query_facts(WS, predicate="based_in", at=_dt(2021))
    assert [f.value for f in facts] == ["Berlin"]
    berlin = facts[0]
    assert berlin.end_derived is True
    assert berlin.valid_to == _dt(2023)


@pytest.mark.usefixtures("based_in")
def test_assert_returns_the_live_closed_copy_not_the_dead_row(
    service: WorldModelService, session: Session
) -> None:
    # M4: Berlin (2020) asserted after Paris (2022) is closed in the same
    # transaction; the result must be the live closed copy.
    service.assert_input(
        WS, _based("Acme", "Paris", valid_from="2022"), source_kind=PERSON, actor="u1"
    )
    result = service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    assert result.created is True
    assert result.fact.value == "Berlin"
    assert result.fact.invalidated_at is None
    assert result.fact.valid_to == _dt(2022)
    assert result.fact.end_derived is True
    # The returned id is usable, not a tombstoned row.
    service.retract(WS, result.fact.id, actor="u1")
    session.commit()


@pytest.mark.usefixtures("based_in")
def test_reasserting_a_closed_fact_is_a_duplicate_not_an_orphan(
    service: WorldModelService, session: Session
) -> None:
    # M5: the closed form must be deduped before insert.
    first = service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    service.assert_input(
        WS, _based("Acme", "Paris", valid_from="2022"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    subject_id = first.fact.subject_id
    closed = service.query_facts(WS, predicate="based_in", at=_dt(2021))[0]
    assert closed.value == "Berlin" and closed.end_derived is True

    def _row_count() -> int:
        return len(
            [
                fact
                for fact in service.repository.facts_touching(WS, [subject_id])
                if fact.predicate_key == "based_in"
            ]
        )

    before = _row_count()
    again = service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    assert again.created is False
    assert again.fact.id == closed.id
    assert _row_count() == before


@pytest.mark.usefixtures("based_in")
def test_unknown_start_pair_is_reported_not_closed(
    service: WorldModelService, session: Session
) -> None:
    service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    service.assert_input(
        WS, _based("Acme", "Munich", valid_from=None), source_kind=PERSON, actor="u1"
    )
    session.commit()
    facts = service.query_facts(WS, predicate="based_in", at=_dt(2021))
    by_value = {fact.value: fact for fact in facts}
    assert set(by_value) == {"Berlin", "Munich"}
    for fact in by_value.values():
        assert fact.valid_to is None  # nothing was closed
        assert fact.end_derived is False
    kinds = {conflict.kind for conflict in service.conflicts(WS)}
    assert "unordered_succession" in kinds


@pytest.mark.usefixtures("based_in")
def test_retracting_successor_reopens_derived_end(
    service: WorldModelService, session: Session
) -> None:
    service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    )
    munich = service.assert_input(
        WS, _based("Acme", "Munich", valid_from="2023"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    service.retract(WS, munich.fact.id, actor="u1")
    session.commit()
    facts = service.query_facts(WS, predicate="based_in", at=_dt(2024))
    assert [f.value for f in facts] == ["Berlin"]
    assert facts[0].valid_to is None


@pytest.mark.parametrize(
    "order", [(0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)]
)
def test_functional_and_inverse_closing_any_arrival_order(
    service: WorldModelService, session: Session, order: tuple[int, int, int]
) -> None:
    # M8: A (s1->o1, 2020) is succeeded on the subject side by B (2022)
    # and on the object side by C (2021); A must end at 2021 in all orders.
    service.create_relation(
        WS,
        key="linked_to",
        label="linked to",
        kind=RelationKind.STATE,
        functional=True,
        inverse_functional=True,
    )
    session.commit()
    claims = [
        ("S One", "O One", "2020"),
        ("S One", "O Two", "2022"),
        ("S Two", "O One", "2021"),
    ]
    for index in order:
        subject, target, year = claims[index]
        service.assert_input(
            WS,
            StatementInput(
                phrase="linked to",
                subject_name=subject,
                predicate_key="linked_to",
                object_name=target,
                valid_from_text=year,
            ),
            source_kind=PERSON,
            actor="u1",
        )
    session.commit()
    at_2020 = service.query_facts(WS, predicate="linked_to", at=_dt(2020, 6, 1))
    assert len(at_2020) == 1
    assert at_2020[0].valid_to == _dt(2021)
    assert at_2020[0].end_derived is True


def test_event_collapses_and_eternal_strips_dates(
    service: WorldModelService, session: Session
) -> None:
    service.create_relation(
        WS, key="founded", label="founded", kind=RelationKind.EVENT, range_class="value"
    )
    service.create_relation(
        WS, key="is_a", label="is a", kind=RelationKind.ETERNAL, range_class="value"
    )
    session.commit()
    founded = service.assert_input(
        WS,
        StatementInput(
            phrase="founded",
            subject_name="Acme",
            predicate_key="founded",
            value="yes",
            valid_from_text="1990",
        ),
        source_kind=PERSON,
        actor="u1",
    )
    eternal = service.assert_input(
        WS,
        StatementInput(
            phrase="is a",
            subject_name="Acme",
            predicate_key="is_a",
            value="company",
            valid_from_text="1990",
        ),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    assert founded.fact.valid_to == _dt(1991)  # one year-bucket
    assert eternal.fact.valid_from is None
    assert eternal.fact.valid_to is None


# --- ontology guards ----------------------------------------------------------- #


def test_system_relation_key_cannot_be_redeclared(service: WorldModelService) -> None:
    # M11: "known_as" is shadowed by the duplicate check, permanently.
    with pytest.raises(ConflictError):
        service.create_relation(
            WS,
            key="known_as",
            label="known as",
            kind=RelationKind.EVENT,
            functional=True,
        )


def test_class_listing_itself_as_parent_message(service: WorldModelService) -> None:
    with pytest.raises(WorldValidationError, match="lists itself as parent"):
        service.create_class(WS, key="org", label="Org", parent_key="org")


def test_entity_type_keys_are_validated(service: WorldModelService, session: Session) -> None:
    # Minor 8: a malformed or unknown type key must never be stored.
    with pytest.raises(WorldValidationError):
        service.assert_input(
            WS,
            StatementInput(
                phrase="leads", subject_name="Ada", subject_type="Not A Key!", value="team"
            ),
            source_kind=PERSON,
            actor="u1",
        )
    with pytest.raises(WorldValidationError):
        service.assert_input(
            WS,
            StatementInput(phrase="leads", subject_name="Ada", subject_type="ghost", value="team"),
            source_kind=PERSON,
            actor="u1",
        )
    session.rollback()
    with pytest.raises(WorldValidationError):
        service.create_proposal(
            WS,
            StatementInput(phrase="leads", subject_name="Ada", subject_type="ghost", value="team"),
            proposer_kind=SourceKind.AGENT,
            proposer="agent-1",
        )


# --- identity, merges, profiles ---------------------------------------------- #


def _two_entities(service: WorldModelService, session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    a = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Acme Inc", value="widgets"),
        source_kind=PERSON,
        actor="u1",
    )
    b = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Acme Subsidiary", value="gadgets"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    return a.fact.subject_id, b.fact.subject_id


def test_suffix_variant_recalls_same_entity(service: WorldModelService, session: Session) -> None:
    first = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Acme Inc", value="widgets"),
        source_kind=PERSON,
        actor="u1",
    )
    second = service.assert_input(
        WS,
        StatementInput(phrase="ships", subject_name="Acme", value="crates"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    assert first.fact.subject_id == second.fact.subject_id
    profile = service.get_entity_profile(WS, first.fact.subject_id)
    assert set(profile.names) == {"Acme Inc", "Acme"}


def test_exact_name_recalls_over_older_suffix_match(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    # Minor 7: "Acme" exactly matches the newer entity; the older
    # "Acme Inc" only matches after suffix stripping and must lose.
    repo = service.repository
    older = repo.insert_entity(WS, label="Acme Inc", now=clock())
    clock.tick(days=1)
    newer = repo.insert_entity(WS, label="Acme", now=clock())
    session.commit()
    assert older.id != newer.id
    result = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Acme", value="widgets"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    assert result.fact.subject_id == newer.id


def test_merge_resolves_on_read_and_revert_restores(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    survivor, merged = _two_entities(service, session)
    before_merge_as_of = clock.now
    clock.tick(days=1)
    merge = service.merge_entities(
        WS, survivor_id=survivor, merged_id=merged, actor="u1", reason="same company"
    )
    session.commit()

    profile = service.get_entity_profile(WS, merged)
    assert profile.resolved_id == survivor
    values = {fact.value for fact in profile.facts if fact.phrase == "sells"}
    assert values == {"widgets", "gadgets"}  # both entities' facts

    # Before the merge was recorded, the merged entity stood alone.
    before = service.get_entity_profile(WS, merged, as_of=before_merge_as_of)
    assert before.resolved_id == merged

    service.revert_merge(WS, merge.id, actor="u1", reason="mistake")
    session.commit()
    after = service.get_entity_profile(WS, merged)
    assert after.resolved_id == merged


def test_merge_refusals(service: WorldModelService, session: Session) -> None:
    survivor, merged = _two_entities(service, session)
    with pytest.raises(ConflictError):
        service.merge_entities(WS, survivor_id=survivor, merged_id=survivor, actor="u1")
    with pytest.raises(NotFoundError):
        service.merge_entities(WS, survivor_id=survivor, merged_id=uuid.uuid4(), actor="u1")
    service.merge_entities(WS, survivor_id=survivor, merged_id=merged, actor="u1")
    session.commit()
    with pytest.raises(ConflictError):  # already has a live merge
        service.merge_entities(WS, survivor_id=survivor, merged_id=merged, actor="u1")
    third = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Third Co", value="sprockets"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    with pytest.raises(ConflictError):  # survivor itself merged away
        service.merge_entities(WS, survivor_id=merged, merged_id=third.fact.subject_id, actor="u1")


def test_merge_of_disjoint_classes_refused(service: WorldModelService, session: Session) -> None:
    service.create_class(WS, key="person", label="Person")
    service.create_class(WS, key="project", label="Project", disjoint_with=["person"])
    session.commit()
    person = service.assert_input(
        WS,
        StatementInput(phrase="leads", subject_name="Ada", subject_type="person", value="team"),
        source_kind=PERSON,
        actor="u1",
    )
    project = service.assert_input(
        WS,
        StatementInput(phrase="ships", subject_name="Apollo", subject_type="project", value="v1"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    with pytest.raises(ConflictError):
        service.merge_entities(
            WS,
            survivor_id=person.fact.subject_id,
            merged_id=project.fact.subject_id,
            actor="u1",
        )


def test_merge_disjoint_check_covers_whole_closures(
    service: WorldModelService, session: Session
) -> None:
    # Minor 10: person -> untyped -> org must not smuggle a person into
    # an org through an untyped intermediate.
    service.create_class(WS, key="person", label="Person")
    service.create_class(WS, key="org", label="Organisation", disjoint_with=["person"])
    session.commit()

    def _entity(name: str, type_key: str | None) -> uuid.UUID:
        result = service.assert_input(
            WS,
            StatementInput(phrase="sells", subject_name=name, subject_type=type_key, value="x"),
            source_kind=PERSON,
            actor="u1",
        )
        session.commit()
        return result.fact.subject_id

    person = _entity("Ada Person", "person")
    untyped = _entity("Middle Entity", None)
    org = _entity("Globex Org", "org")
    service.merge_entities(WS, survivor_id=untyped, merged_id=person, actor="u1")
    session.commit()
    with pytest.raises(ConflictError):
        service.merge_entities(WS, survivor_id=org, merged_id=untyped, actor="u1")


def test_person_set_type_is_final_for_models(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    service.create_class(WS, key="person", label="Person")
    service.create_class(WS, key="org", label="Organisation")
    session.commit()
    fact = service.assert_input(
        WS,
        StatementInput(phrase="leads", subject_name="Ada", subject_type="person", value="team"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    repo = service.repository
    entity = repo.set_entity_type(
        WS, fact.fact.subject_id, type_key="org", source="model", now=clock()
    )
    assert entity.type_key == "person"  # model overwrite refused
    entity = repo.set_entity_type(
        WS, fact.fact.subject_id, type_key="org", source="person", now=clock()
    )
    assert entity.type_key == "org"  # a person may retype


def test_conflicts_are_merge_aware_on_subject_side(
    service: WorldModelService, session: Session
) -> None:
    # M9: two CEOs on two entities become one conflict once merged.
    service.create_relation(
        WS,
        key="has_ceo",
        label="has CEO",
        kind=RelationKind.STATE,
        functional=True,
    )
    session.commit()

    def _ceo(org: str, person: str) -> uuid.UUID:
        result = service.assert_input(
            WS,
            StatementInput(
                phrase="has CEO",
                subject_name=org,
                predicate_key="has_ceo",
                object_name=person,
                valid_from_text="2020",
            ),
            source_kind=PERSON,
            actor="u1",
        )
        session.commit()
        return result.fact.subject_id

    ltd = _ceo("Acme Ltd", "Ann")
    holdings = _ceo("Acme Holdings", "Bob")
    overlaps = [c for c in service.conflicts(WS) if c.kind == "functional_overlap"]
    assert overlaps == []

    merge = service.merge_entities(WS, survivor_id=holdings, merged_id=ltd, actor="u1")
    session.commit()
    overlaps = [c for c in service.conflicts(WS) if c.kind == "functional_overlap"]
    assert len(overlaps) == 1
    assert overlaps[0].subject_id == holdings

    service.revert_merge(WS, merge.id, actor="u1", reason="undo")
    session.commit()
    overlaps = [c for c in service.conflicts(WS) if c.kind == "functional_overlap"]
    assert overlaps == []


def test_conflicts_are_merge_aware_on_object_side(
    service: WorldModelService, session: Session
) -> None:
    # M9 (object side): merging "A. Smith" into "Ann Smith" clears the
    # apparent two-CEO conflict.
    service.create_relation(
        WS,
        key="has_ceo",
        label="has CEO",
        kind=RelationKind.STATE,
        functional=True,
    )
    session.commit()

    def _ceo(person: str) -> uuid.UUID:
        result = service.assert_input(
            WS,
            StatementInput(
                phrase="has CEO",
                subject_name="Acme",
                predicate_key="has_ceo",
                object_name=person,
                valid_from_text="2020",
            ),
            source_kind=PERSON,
            actor="u1",
        )
        session.commit()
        assert result.fact.object_id is not None
        return result.fact.object_id

    ann = _ceo("Ann Smith")
    a_smith = _ceo("A. Smith")
    overlaps = [c for c in service.conflicts(WS) if c.kind == "functional_overlap"]
    assert len(overlaps) == 1

    service.merge_entities(WS, survivor_id=ann, merged_id=a_smith, actor="u1")
    session.commit()
    overlaps = [c for c in service.conflicts(WS) if c.kind == "functional_overlap"]
    assert overlaps == []


@pytest.mark.usefixtures("based_in")
def test_entity_changes_record_axis(
    service: WorldModelService, clock: FakeClock, session: Session
) -> None:
    first = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    session.commit()
    clock.tick(days=2)
    service.retract(WS, first.fact.id, actor="u1")
    session.commit()
    events = service.entity_changes(WS, first.fact.subject_id)
    kinds = [(event.kind, event.reason) for event in events if event.fact_id == first.fact.id]
    assert kinds == [("asserted", None), ("invalidated", "retracted")]
    recent = service.entity_changes(WS, first.fact.subject_id, since=clock.now, until=None)
    assert all(event.kind == "invalidated" for event in recent)


# --- proposals (criterion 5) --------------------------------------------------- #


@pytest.mark.usefixtures("based_in")
def test_proposal_lifecycle_and_shadow_gate(service: WorldModelService, session: Session) -> None:
    proposal = service.create_proposal(
        WS,
        _based("Acme", "Berlin"),
        proposer_kind=SourceKind.AGENT,
        proposer="agent-1",
        rationale="the annual report says so",
        verdict="accept",
        confidence=0.9,
    )
    session.commit()
    assert proposal.gate["decision"] == "auto"  # recorded, never applied
    assert service.query_facts(WS, predicate="based_in") == []

    outcome = service.decide_proposal(WS, proposal.id, decider="person-1", accept=True)
    session.commit()
    assert outcome.fact is not None
    assert outcome.fact.proposal_id == proposal.id
    assert outcome.fact.source_kind is SourceKind.AGENT
    assert outcome.fact.actor == "agent-1"
    assert outcome.proposal.status.value == "accepted"

    with pytest.raises(ConflictError):  # decided twice
        service.decide_proposal(WS, proposal.id, decider="person-2", accept=True)


@pytest.mark.usefixtures("based_in")
def test_proposer_cannot_decide_and_reject_needs_reason(
    service: WorldModelService, session: Session
) -> None:
    proposal = service.create_proposal(
        WS,
        _based("Acme", "Berlin"),
        proposer_kind=SourceKind.PERSON,
        proposer="person-1",
    )
    session.commit()
    with pytest.raises(ForbiddenError):
        service.decide_proposal(WS, proposal.id, decider="person-1", accept=True)
    with pytest.raises(WorldValidationError):
        service.decide_proposal(WS, proposal.id, decider="person-2", accept=False)
    outcome = service.decide_proposal(
        WS, proposal.id, decider="person-2", accept=False, reason="stale source"
    )
    session.commit()
    assert outcome.fact is None
    assert outcome.proposal.status.value == "rejected"
    assert service.query_facts(WS, predicate="based_in") == []


# --- concurrent transitions are 409s, not overwrites (M6, minor 15) ------------ #
#
# Each test simulates a lost race the same way: the row is loaded into the
# session's identity map, then an unsynchronised UPDATE (the "other transaction")
# changes it underneath. A read-check-write implementation acts on the
# stale snapshot; a conditional UPDATE sees the real row and refuses.


@pytest.mark.usefixtures("based_in")
def test_decide_proposal_losing_a_race_conflicts(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    proposal = service.create_proposal(
        WS,
        _based("Acme", "Berlin"),
        proposer_kind=SourceKind.AGENT,
        proposer="agent-1",
    )
    session.commit()
    repo = service.repository
    assert repo.get_proposal(WS, proposal.id) is not None  # cached as proposed
    session.execute(
        update(WorldProposal)
        .where(WorldProposal.id == proposal.id)
        .values(status="rejected", decided_by="other")
        .execution_options(synchronize_session=False)
    )
    session.flush()
    with pytest.raises(ConflictError):
        repo.decide_proposal(
            WS,
            proposal.id,
            accept=True,
            decided_by="person-1",
            reason=None,
            fact_id=None,
            now=clock(),
        )
    session.rollback()


@pytest.mark.usefixtures("based_in")
def test_invalidate_fact_losing_a_race_conflicts(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    result = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    session.commit()
    repo = service.repository
    assert repo.get_fact(WS, result.fact.id) is not None  # cached as live
    session.execute(
        update(WorldFact)
        .where(WorldFact.id == result.fact.id)
        .values(invalidated_at=clock(), invalidated_reason="retracted")
        .execution_options(synchronize_session=False)
    )
    session.flush()
    with pytest.raises(ConflictError):
        repo.invalidate_fact(WS, result.fact.id, reason=InvalidatedReason.SUPERSEDED, now=clock())
    session.rollback()


def test_revert_merge_losing_a_race_conflicts(
    service: WorldModelService, session: Session, clock: FakeClock
) -> None:
    survivor, merged = _two_entities(service, session)
    merge = service.merge_entities(WS, survivor_id=survivor, merged_id=merged, actor="u1")
    session.commit()
    repo = service.repository
    assert repo.get_merge(WS, merge.id) is not None  # cached as live
    session.execute(
        update(WorldEntityMerge)
        .where(WorldEntityMerge.id == merge.id)
        .values(reverted_at=clock(), reverted_by="other")
        .execution_options(synchronize_session=False)
    )
    session.flush()
    with pytest.raises(ConflictError):
        repo.revert_merge(WS, merge.id, reverted_by="person-1", reason="mine", now=clock())
    session.rollback()


# --- reasoning (criterion 6) --------------------------------------------------- #


def _entity_relation(service: WorldModelService, session: Session, key: str, **flags: bool) -> None:
    service.create_relation(WS, key=key, label=key, kind=RelationKind.STATE, **flags)
    session.commit()


def test_derive_writes_consequences_with_proofs_and_is_idempotent(
    service: WorldModelService, session: Session
) -> None:
    _entity_relation(service, session, "part_of", transitive=True)
    _entity_relation(service, session, "partner_of", symmetric=True)

    def link(subject: str, predicate: str, target: str) -> uuid.UUID:
        result = service.assert_input(
            WS,
            StatementInput(
                phrase=predicate,
                subject_name=subject,
                predicate_key=predicate,
                object_name=target,
            ),
            source_kind=PERSON,
            actor="u1",
        )
        return result.fact.id

    ab = link("Alpha", "part_of", "Beta")
    link("Beta", "part_of", "Gamma")
    link("Alpha", "partner_of", "Beta")
    session.commit()

    result = service.derive(WS, actor="u1")
    session.commit()
    assert result.created == 2  # Alpha part_of Gamma; Beta partner_of Alpha
    assert result.violations == ()

    derived = service.query_facts(WS, layer=Layer.DERIVED)
    assert len(derived) == 2
    assert all(fact.source_kind is SourceKind.RULE for fact in derived)

    proofs = service.repository.derivations_for(WS, [fact.id for fact in derived])
    rules = {record.rule for records in proofs.values() for record in records}
    assert rules == {"transitive", "symmetric"}
    transitive_proof = next(
        record for records in proofs.values() for record in records if record.rule == "transitive"
    )
    assert ab in transitive_proof.premise_ids

    again = service.derive(WS, actor="u1")
    session.commit()
    assert again.created == 0
    assert again.invalidated == 0  # second run is a no-op


def test_derive_runs_all_four_rules_and_reports_violations(
    service: WorldModelService, session: Session
) -> None:
    # Minor 21: inverse, sub-property and asserted-beats-derived through
    # the service (not just the pure functions), relations declared
    # target-first so their references resolve.
    _entity_relation(service, session, "part_of", transitive=True)
    _entity_relation(service, session, "partner_of", symmetric=True)
    _entity_relation(service, session, "parent_of")
    _entity_relation(service, session, "child_of", inverse_of="parent_of")
    _entity_relation(service, session, "works_at", functional=True)
    _entity_relation(service, session, "ceo_of", sub_property_of="works_at")

    def link(subject: str, predicate: str, target: str) -> None:
        service.assert_input(
            WS,
            StatementInput(
                phrase=predicate,
                subject_name=subject,
                predicate_key=predicate,
                object_name=target,
            ),
            source_kind=PERSON,
            actor="u1",
        )

    link("Alpha", "part_of", "Beta")
    link("Beta", "part_of", "Gamma")  # transitive: Alpha part_of Gamma
    link("Delta", "partner_of", "Epsilon")  # symmetric: Epsilon partner_of Delta
    link("Gus", "child_of", "Fay")  # inverse: Fay parent_of Gus
    link("Ann", "ceo_of", "Acme")  # sub-property: Ann works_at Acme
    link("Bob", "ceo_of", "Globex")  # would derive Bob works_at Globex...
    link("Bob", "works_at", "Acme")  # ...but asserted functional wins
    session.commit()

    result = service.derive(WS, actor="u1")
    session.commit()
    assert result.created == 4
    assert len(result.violations) == 1
    assert result.violations[0].predicate_key == "works_at"

    derived = service.query_facts(WS, layer=Layer.DERIVED)
    proofs = service.repository.derivations_for(WS, [fact.id for fact in derived])
    rules = {record.rule for records in proofs.values() for record in records}
    assert rules == {"transitive", "symmetric", "inverse", "sub_property"}

    again = service.derive(WS, actor="u1")
    session.commit()
    assert again.created == 0
    assert again.invalidated == 0
    assert len(again.violations) == 1  # reported again, still not a fact


def test_assert_reports_created_and_typed_entities(
    service: WorldModelService, session: Session
) -> None:
    # Minor 14 (service half): entity side effects ride on AssertResult
    # so the router can audit them.
    service.create_class(WS, key="person", label="Person")
    service.create_class(WS, key="org", label="Organisation")
    session.commit()
    result = service.assert_input(
        WS,
        StatementInput(
            phrase="works at",
            subject_name="Ada",
            subject_type="person",
            object_name="Acme",
            object_type="org",
        ),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    assert set(result.created_entity_ids) == {result.fact.subject_id, result.fact.object_id}
    assert set(result.typed_entity_ids) == {result.fact.subject_id, result.fact.object_id}

    again = service.assert_input(
        WS,
        StatementInput(phrase="leads", subject_name="Ada", value="team"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    assert again.created_entity_ids == ()
    assert again.typed_entity_ids == ()


def test_derive_underives_after_premise_retracted(
    service: WorldModelService, session: Session
) -> None:
    _entity_relation(service, session, "partner_of", symmetric=True)
    premise = service.assert_input(
        WS,
        StatementInput(
            phrase="partner of",
            subject_name="Alpha",
            predicate_key="partner_of",
            object_name="Beta",
        ),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    first = service.derive(WS, actor="u1")
    session.commit()
    assert first.created == 1

    service.retract(WS, premise.fact.id, actor="u1")
    session.commit()
    second = service.derive(WS, actor="u1")
    session.commit()
    assert second.created == 0
    assert second.invalidated == 1
    assert service.query_facts(WS, layer=Layer.DERIVED) == []


# --- decisions and drift (criterion 7) ----------------------------------------- #


@pytest.mark.usefixtures("based_in")
def test_decision_drift_names_successor(service: WorldModelService, session: Session) -> None:
    ids = []
    for value, year in (("Berlin", "2020"), ("Munich", "2021"), ("Hamburg", "2022")):
        result = service.assert_input(
            WS,
            StatementInput(
                phrase="sells",
                subject_name=f"Co {value}",
                value=value,
                valid_from_text=year,
            ),
            source_kind=PERSON,
            actor="u1",
        )
        ids.append(result.fact.id)
    session.commit()
    decision = service.record_decision(
        WS,
        title="Pick suppliers",
        outcome="approved",
        actor="u1",
        fact_ids=ids,
        links={"spec_key": "SPEC-1"},
    )
    session.commit()
    assert service.decision_drift(WS, decision.id).drifted is False

    successor = service.supersede(
        WS,
        ids[0],
        StatementInput(
            phrase="sells",
            subject_name="Co Berlin",
            value="Potsdam",
            valid_from_text="2020",
        ),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    report = service.decision_drift(WS, decision.id)
    assert report.drifted is True
    drifted = [item for item in report.items if item.status != "unchanged"]
    assert len(drifted) == 1
    assert drifted[0].fact_id == ids[0]
    assert drifted[0].status == "superseded"
    assert drifted[0].superseded_by == successor.fact.id


def test_supersede_duplicating_another_live_fact_is_refused(
    service: WorldModelService, session: Session
) -> None:
    # Minor 1: a correction that dedups onto another live fact gets no
    # supersedes_id row, so it is refused instead of silently drifting.
    widgets = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Acme", value="widgets"),
        source_kind=PERSON,
        actor="u1",
    )
    gadgets = service.assert_input(
        WS,
        StatementInput(phrase="sells", subject_name="Acme", value="gadgets"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    with pytest.raises(ConflictError, match="retract instead"):
        service.supersede(
            WS,
            widgets.fact.id,
            StatementInput(phrase="sells", subject_name="Acme", value="gadgets"),
            source_kind=PERSON,
            actor="u1",
        )
    session.rollback()
    # The old fact still stands once the refusal rolls the write back.
    assert service.get_fact(WS, widgets.fact.id).invalidated_at is None
    assert service.get_fact(WS, gadgets.fact.id).invalidated_at is None


@pytest.mark.usefixtures("based_in")
def test_decision_drift_names_live_successor_of_closed_correction(
    service: WorldModelService, session: Session
) -> None:
    # Minor 2: the direct successor (Munich) is later tombstoned by a
    # temporal closure; drift must name its live closed copy.
    berlin = service.assert_input(
        WS, _based("Acme", "Berlin", valid_from="2020"), source_kind=PERSON, actor="u1"
    ).fact
    session.commit()
    decision = service.record_decision(WS, title="HQ pick", actor="u1", fact_ids=[berlin.id])
    session.commit()

    corrected = service.supersede(
        WS,
        berlin.id,
        _based("Acme", "Munich", valid_from="2020"),
        source_kind=PERSON,
        actor="u1",
    )
    session.commit()
    assert corrected.fact.value == "Munich"
    service.assert_input(
        WS, _based("Acme", "Paris", valid_from="2022"), source_kind=PERSON, actor="u1"
    )
    session.commit()
    closed_munich = service.query_facts(WS, predicate="based_in", at=_dt(2021))[0]
    assert closed_munich.value == "Munich"
    assert closed_munich.valid_to == _dt(2022)
    assert closed_munich.id != corrected.fact.id  # the direct successor is dead
    report = service.decision_drift(WS, decision.id)
    [item] = report.items
    assert item.status == "superseded"
    assert item.superseded_by == closed_munich.id
    named = service.get_fact(WS, item.superseded_by) if item.superseded_by else None
    assert named is not None and named.invalidated_at is None


def test_record_decision_with_unknown_fact_is_not_found(
    service: WorldModelService,
) -> None:
    with pytest.raises(NotFoundError):
        service.record_decision(WS, title="x", actor="u1", fact_ids=[uuid.uuid4()])


# --- ingest (criterion 3) ------------------------------------------------------- #


def _reply(statements: list[dict[str, object]]) -> str:
    return json.dumps({"statements": statements})


def _ingest_service(
    session: Session, clock: FakeClock, script: list[str | Exception]
) -> tuple[WorldModelService, FakeModelClient]:
    client = FakeModelClient(script)
    return (
        WorldModelService(WorldRepository(session), model_client=client, clock=clock),
        client,
    )


def test_ingest_without_model_refuses(service: WorldModelService) -> None:
    with pytest.raises(ModelNotConfiguredError):
        service.ingest(WS, source_ref="doc1", title="T", text="hello", actor="u1")


def test_ingest_writes_only_verbatim_with_server_offsets(
    session: Session, clock: FakeClock
) -> None:
    text = "Acme moved to Berlin in 2020. Globex stayed in Paris."
    good = {
        "quote": "Acme moved to Berlin in 2020",
        "subject": "Acme",
        "subject_kind": "named",
        "phrase": "moved to",
        "object": None,
        "value": "Berlin",
        "object_kind": "value",
        "time": {"from": "2020", "to": None, "words": "in 2020"},
        "mood": None,
        "qualifiers": {},
        # Model-sent offsets are ignored; the server computes its own.
        "start_offset": 999,
        "end_offset": 1005,
    }
    invented = dict(good, quote="Acme moved to London in 2020", value="London")
    bad_time = dict(
        good,
        quote="Globex stayed in Paris",
        subject="Globex",
        phrase="stayed in",
        value="Paris",
        time={"from": "1999", "to": None, "words": "in 1999"},
    )
    svc, client = _ingest_service(session, clock, [_reply([good, invented, bad_time])])
    result = svc.ingest(WS, source_ref="report.md", title="Report", text=text, actor="u1")
    session.commit()

    assert client.requests and client.requests[0].temperature == 0.0
    assert result.facts_created == 1  # extracted facts (known_as is bookkeeping)
    assert result.entities_created == 1
    assert result.drops == 2
    assert result.drops_by_reason == {"quote_not_in_chunk": 1, "time_not_in_quote": 1}
    assert sorted(svc.repository.list_drops(WS, source_ref="report.md")) == [
        ("report.md", 0, "quote_not_in_chunk"),
        ("report.md", 0, "time_not_in_quote"),
    ]

    evidence = svc.evidence_for(WS, list(result.fact_ids))
    all_evidence = [item for items in evidence.values() for item in items]
    assert all_evidence
    for item in all_evidence:
        chunk_text = text  # one chunk: offsets index into it
        assert chunk_text[item.start_offset : item.end_offset] == item.quote


def test_ingest_chunk_failure_is_counted_and_others_continue(
    session: Session, clock: FakeClock
) -> None:
    text = "First paragraph about nothing.\n\nAcme moved to Berlin in 2020."
    good = {
        "quote": "Acme moved to Berlin in 2020",
        "subject": "Acme",
        "subject_kind": "named",
        "phrase": "moved to",
        "object": None,
        "value": "Berlin",
        "object_kind": "value",
        "time": {"from": "2020", "to": None, "words": "in 2020"},
        "mood": None,
        "qualifiers": {},
    }
    svc, _ = _ingest_service(session, clock, [RuntimeError("provider exploded"), _reply([good])])
    result = svc.ingest(WS, source_ref="doc.md", title="Doc", text=text, actor="u1")
    session.commit()
    assert result.chunks == 2
    assert result.drops_by_reason == {"chunk_unextracted": 1}
    assert result.facts_created >= 1


def test_ingest_described_entities_are_never_recalled(session: Session, clock: FakeClock) -> None:
    text = "The vendor supplies Acme with widgets."
    stmt = {
        "quote": "The vendor supplies Acme with widgets",
        "subject": "the vendor",
        "subject_kind": "described",
        "phrase": "supplies",
        "object": "Acme",
        "value": None,
        "object_kind": "entity",
        "time": None,
        "mood": None,
        "qualifiers": {},
    }
    svc, _ = _ingest_service(session, clock, [_reply([stmt]), _reply([stmt])])
    for ref in ("doc-a.md", "doc-b.md"):
        svc.ingest(WS, source_ref=ref, title="Doc", text=text, actor="u1")
        session.commit()
    described = [entity for entity in svc.repository.list_entities(WS) if entity.described]
    assert len(described) == 2  # one per document, never matched together
    assert all(entity.described_in in {"doc-a.md", "doc-b.md"} for entity in described)


def test_ingest_described_subject_reused_within_its_document(
    session: Session, clock: FakeClock
) -> None:
    # M7: one described entity per (document, description), and a
    # re-ingest of the same document creates none.
    text = "The vendor ships widgets. The vendor is based in Oslo."
    ships = {
        "quote": "The vendor ships widgets",
        "subject": "the vendor",
        "subject_kind": "described",
        "phrase": "ships",
        "object": None,
        "value": "widgets",
        "object_kind": "value",
        "time": None,
        "mood": None,
        "qualifiers": {},
    }
    based = {
        "quote": "The vendor is based in Oslo",
        "subject": "the vendor",
        "subject_kind": "described",
        "phrase": "is based in",
        "object": None,
        "value": "Oslo",
        "object_kind": "value",
        "time": None,
        "mood": None,
        "qualifiers": {},
    }
    svc, _ = _ingest_service(session, clock, [_reply([ships, based]), _reply([ships, based])])
    first = svc.ingest(WS, source_ref="doc.md", title="Doc", text=text, actor="u1")
    session.commit()
    described = [e for e in svc.repository.list_entities(WS) if e.described]
    assert len(described) == 1
    assert first.entities_created == 1

    second = svc.ingest(WS, source_ref="doc.md", title="Doc", text=text, actor="u1")
    session.commit()
    assert second.entities_created == 0
    assert len([e for e in svc.repository.list_entities(WS) if e.described]) == 1


def test_ingest_described_objects_are_per_document(session: Session, clock: FakeClock) -> None:
    # M3: a described object is a per-document described entity, never a
    # shared named entity recalled across documents.
    stmt_for = {
        "Alice": {
            "quote": "Alice works for the vendor",
            "subject": "Alice",
            "subject_kind": "named",
            "phrase": "works for",
            "object": "the vendor",
            "value": None,
            "object_kind": "described",
            "time": None,
            "mood": None,
            "qualifiers": {},
        },
        "Bob": {
            "quote": "Bob works for the vendor",
            "subject": "Bob",
            "subject_kind": "named",
            "phrase": "works for",
            "object": "the vendor",
            "value": None,
            "object_kind": "described",
            "time": None,
            "mood": None,
            "qualifiers": {},
        },
    }
    svc, _ = _ingest_service(
        session, clock, [_reply([stmt_for["Alice"]]), _reply([stmt_for["Bob"]])]
    )
    fact_ids: list[uuid.UUID] = []
    for ref, name in (("doc-a.md", "Alice"), ("doc-b.md", "Bob")):
        result = svc.ingest(
            WS,
            source_ref=ref,
            title="Doc",
            text=f"{name} works for the vendor.",
            actor="u1",
        )
        session.commit()
        fact_ids.extend(result.fact_ids)
    described = [e for e in svc.repository.list_entities(WS) if e.described]
    assert len(described) == 2
    facts = svc.repository.facts_by_ids(WS, fact_ids)
    assert {fact.object_id for fact in facts.values()} == {e.id for e in described}


def test_ingest_types_only_non_state_phrase_matches(session: Session, clock: FakeClock) -> None:
    setup = WorldModelService(WorldRepository(session), clock=clock)
    setup.create_relation(
        WS,
        key="founded",
        label="founded",
        kind=RelationKind.EVENT,
        range_class="value",
    )
    setup.create_relation(
        WS,
        key="based_in",
        label="based in",
        kind=RelationKind.STATE,
        range_class="value",
        functional=True,
    )
    session.commit()
    text = "Acme was founded in 1990. Acme is based in Berlin."
    founded = {
        "quote": "Acme was founded in 1990",
        "subject": "Acme",
        "subject_kind": "named",
        "phrase": "founded",
        "object": None,
        "value": "1990",
        "object_kind": "value",
        "time": {"from": "1990", "to": None, "words": "in 1990"},
        "mood": None,
        "qualifiers": {},
    }
    based = {
        "quote": "Acme is based in Berlin",
        "subject": "Acme",
        "subject_kind": "named",
        "phrase": "based in",
        "object": None,
        "value": "Berlin",
        "object_kind": "value",
        "time": None,
        "mood": None,
        "qualifiers": {},
    }
    svc, _ = _ingest_service(session, clock, [_reply([founded, based])])
    result = svc.ingest(WS, source_ref="doc.md", title="Doc", text=text, actor="u1")
    session.commit()
    facts = svc.repository.facts_by_ids(WS, list(result.fact_ids))
    by_phrase = {fact.phrase: fact for fact in facts.values()}
    assert by_phrase["founded"].predicate_key == "founded"
    assert by_phrase["founded"].layer is Layer.TYPED
    assert by_phrase["founded"].valid_to == _dt(1991)  # event bucket
    assert by_phrase["based in"].predicate_key is None  # state stays open
    assert by_phrase["based in"].layer is Layer.OPEN


# --- tenancy and search ---------------------------------------------------------- #


@pytest.mark.usefixtures("based_in")
def test_cross_workspace_isolation(service: WorldModelService, session: Session) -> None:
    result = service.assert_input(WS, _based("Acme", "Berlin"), source_kind=PERSON, actor="u1")
    session.commit()
    assert service.query_facts(WS2, predicate="based_in") == []
    with pytest.raises(NotFoundError):
        service.get_fact(WS2, result.fact.id)
    with pytest.raises(NotFoundError):
        service.get_entity_profile(WS2, result.fact.subject_id)


@pytest.mark.usefixtures("based_in")
def test_search_entities_by_name_and_type(service: WorldModelService, session: Session) -> None:
    service.assert_input(WS, _based("Acme Widgets", "Berlin"), source_kind=PERSON, actor="u1")
    service.assert_input(WS, _based("Globex", "Paris"), source_kind=PERSON, actor="u1")
    session.commit()
    found = service.search_entities(WS, q="acme")
    assert [entity.label for entity in found] == ["Acme Widgets"]
    assert service.search_entities(WS, q="nothinghere") == []
