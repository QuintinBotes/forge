"""Functional conflicts, computed on read from the live fact set.

Pure module — no I/O. Two live values of a functional relation for one
subject (or of an inverse-functional relation for one object) whose world
intervals overlap are a conflict; a clean succession is not. For STATE
relations, pairs whose ordering is unknowable (an unknown start) are
reported by temporal closing as unordered successions instead, so they
are skipped here; closing never runs for other kinds, so unknown-start
pairs of eternal/event relations are reported here as overlaps.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from forge_world.ledger import (
    FactData,
    RelationDef,
    RelationKind,
    normalise_phrase,
    normalise_value,
)
from forge_world.time import intervals_overlap


@dataclass(frozen=True)
class Conflict:
    """One contradiction a person should look at."""

    kind: str
    predicate_key: str | None
    subject_id: uuid.UUID | None
    object_id: uuid.UUID | None
    fact_ids: tuple[uuid.UUID, ...]
    detail: str


def predicate_identity(fact: FactData) -> str:
    """Grouping identity of a fact's predicate (key, else normalised phrase)."""
    return fact.predicate_key or f"phrase:{normalise_phrase(fact.phrase)}"


def value_identity(fact: FactData) -> tuple[str, str]:
    """Grouping identity of a fact's object side (entity or literal)."""
    if fact.object_id is not None:
        return ("entity", str(fact.object_id))
    return ("value", normalise_value(fact.value or ""))


def _overlapping_pairs(
    group: list[FactData], *, differ: str, skip_unknown: bool
) -> list[tuple[FactData, FactData]]:
    pairs: list[tuple[FactData, FactData]] = []
    ordered = sorted(group, key=lambda f: str(f.id))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if skip_unknown and (a.valid_from is None or b.valid_from is None):
                continue  # unorderable STATE pair: temporal closing reports it
            same = (
                value_identity(a) == value_identity(b)
                if differ == "value"
                else a.subject_id == b.subject_id
            )
            if same:
                continue
            if intervals_overlap(a.valid_from, a.valid_to, b.valid_from, b.valid_to):
                pairs.append((a, b))
    return pairs


def functional_conflicts(
    facts: Sequence[FactData],
    relations: Mapping[str, RelationDef] | Sequence[RelationDef],
) -> list[Conflict]:
    """Overlapping distinct values of functional (inverse-) relations."""
    by_key: Mapping[str, RelationDef] = (
        relations
        if isinstance(relations, Mapping)
        else {relation.key: relation for relation in relations}
    )
    live = [
        fact
        for fact in facts
        if fact.invalidated_at is None
        and fact.mood is None
        and fact.predicate_key is not None
        and fact.predicate_key in by_key
    ]

    conflicts: list[Conflict] = []
    functional_groups: dict[tuple[uuid.UUID, str], list[FactData]] = {}
    inverse_groups: dict[tuple[tuple[str, str], str], list[FactData]] = {}
    for fact in live:
        relation = by_key[fact.predicate_key or ""]
        predicate = predicate_identity(fact)
        if relation.functional:
            functional_groups.setdefault((fact.subject_id, predicate), []).append(fact)
        if relation.inverse_functional:
            inverse_groups.setdefault((value_identity(fact), predicate), []).append(fact)

    for (subject_id, _), group in sorted(
        functional_groups.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        for a, b in _overlapping_pairs(
            group,
            differ="value",
            skip_unknown=by_key[group[0].predicate_key or ""].kind is RelationKind.STATE,
        ):
            conflicts.append(
                Conflict(
                    kind="functional_overlap",
                    predicate_key=a.predicate_key,
                    subject_id=subject_id,
                    object_id=None,
                    fact_ids=tuple(sorted((a.id, b.id), key=str)),
                    detail=(
                        f"two values of functional relation {a.predicate_key!r} "
                        "hold at the same world time"
                    ),
                )
            )
    for (object_identity, _), group in sorted(
        inverse_groups.items(), key=lambda item: (item[0][0][1], item[0][1])
    ):
        for a, b in _overlapping_pairs(
            group,
            differ="subject",
            skip_unknown=by_key[group[0].predicate_key or ""].kind is RelationKind.STATE,
        ):
            conflicts.append(
                Conflict(
                    kind="inverse_functional_overlap",
                    predicate_key=a.predicate_key,
                    subject_id=None,
                    object_id=a.object_id if object_identity[0] == "entity" else None,
                    fact_ids=tuple(sorted((a.id, b.id), key=str)),
                    detail=(
                        f"two subjects of inverse-functional relation "
                        f"{a.predicate_key!r} hold at the same world time"
                    ),
                )
            )
    return conflicts


__all__ = ["Conflict", "functional_conflicts", "predicate_identity", "value_identity"]
