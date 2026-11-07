"""Pure tests for forge_world.ledger (statements, dedup identity, keys)."""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from forge_world import (
    Layer,
    Mood,
    Statement,
    WorldValidationError,
    dedup_key,
    normalise_phrase,
    parse_bound,
    validate_key,
)


def _statement(**overrides: object) -> Statement:
    base = Statement(
        subject_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        phrase="Based   In",
        predicate_key=None,
        object_id=None,
        value="  Berlin ",
        layer=Layer.OPEN,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_normalise_phrase() -> None:
    assert normalise_phrase("  Works   FOR\n Acme ") == "works for acme"


def test_dedup_key_is_stable_and_normalised() -> None:
    a = _statement()
    b = _statement(phrase="based in", value="berlin")
    assert dedup_key(a) == dedup_key(b)  # case/whitespace-insensitive
    assert dedup_key(a) == dedup_key(a)  # stable


def test_dedup_key_prefers_predicate_over_phrase() -> None:
    a = _statement(predicate_key="based_in", phrase="based in", layer=Layer.TYPED)
    b = _statement(predicate_key="based_in", phrase="headquartered in", layer=Layer.TYPED)
    assert dedup_key(a) == dedup_key(b)


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject_id": uuid.uuid4()},
        {"value": "Munich"},
        {"mood": Mood.PLAN},
        {"layer": Layer.MEMORY},
        {"valid_from": parse_bound("2020")},
        {"valid_to": parse_bound("2025", is_end=True)},
        {"predicate_key": "based_in"},
    ],
)
def test_dedup_key_changes_with_each_component(overrides: dict[str, object]) -> None:
    assert dedup_key(_statement(**overrides)) != dedup_key(_statement())


def test_statement_requires_exactly_one_target() -> None:
    with pytest.raises(WorldValidationError):
        Statement(
            subject_id=uuid.uuid4(),
            phrase="knows",
            predicate_key=None,
            object_id=uuid.uuid4(),
            value="x",
            layer=Layer.OPEN,
        )
    with pytest.raises(WorldValidationError):
        Statement(
            subject_id=uuid.uuid4(),
            phrase="knows",
            predicate_key=None,
            object_id=None,
            value=None,
            layer=Layer.OPEN,
        )


def test_statement_rejects_empty_span_and_bad_confidence() -> None:
    with pytest.raises(WorldValidationError):
        Statement(
            subject_id=uuid.uuid4(),
            phrase="based in",
            predicate_key=None,
            object_id=None,
            value="Berlin",
            layer=Layer.OPEN,
            valid_from=parse_bound("2024-01-01"),
            valid_to=parse_bound("2023-01-01", is_end=True),
        )
    with pytest.raises(WorldValidationError):
        _statement(confidence=1.5)


def test_validate_key() -> None:
    assert validate_key("based_in") == "based_in"
    assert validate_key("a" * 40) == "a" * 40
    for bad in ("", "BasedIn", "based-in", "a" * 41, "has space", "abc\n"):
        with pytest.raises(WorldValidationError):
            validate_key(bad)
