"""Temporal closing for functional state relations.

Pure module — no I/O. For one (subject, predicate) timeline of live,
non-mood facts, a fact's derived end is the earliest later start of a
*different* value: a succession closes its predecessor, independent of the
order facts arrived in (the computation is a pure function of the set).
Explicit (non-derived) ends are never rewritten. A pair that cannot be
ordered because one start is unknown closes nothing and is reported as an
unordered-succession conflict for a person. The service applies the diff as
supersessions; this module only computes it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from forge_world.conflicts import Conflict, predicate_identity, value_identity
from forge_world.ledger import FactData
from forge_world.time import Precision, WorldBound, ensure_utc

#: Successor tie-break at one start instant: the finest precision wins, so
#: a derived end's precision never depends on arrival order.
_FINEST_FIRST: dict[Precision, int] = {
    Precision.SECOND: 0,
    Precision.MINUTE: 1,
    Precision.HOUR: 2,
    Precision.DAY: 3,
    Precision.MONTH: 4,
    Precision.YEAR: 5,
    Precision.UNKNOWN: 6,
}


@dataclass(frozen=True)
class Closure:
    """Fact ``fact_id``'s world interval should end at ``new_valid_to``."""

    fact_id: uuid.UUID
    new_valid_to: WorldBound


def _group_key(fact: FactData, side: Literal["subject", "object"]) -> tuple[str, str]:
    anchor = str(fact.subject_id) if side == "subject" else "|".join(value_identity(fact))
    return (anchor, predicate_identity(fact))


def _value_key(fact: FactData, side: Literal["subject", "object"]) -> tuple[str, str]:
    if side == "subject":
        return value_identity(fact)
    return ("entity", str(fact.subject_id))


def _start(fact: FactData) -> datetime:
    """The fact's known start as UTC (call only on known-start facts)."""
    assert fact.valid_from is not None
    return ensure_utc(fact.valid_from)


def _successor_key(fact: FactData) -> tuple[datetime, int, str]:
    return (_start(fact), _FINEST_FIRST[fact.valid_from_precision], str(fact.id))


def compute_ends(
    facts: Sequence[FactData],
    *,
    side: Literal["subject", "object"] = "subject",
    sides: Sequence[Literal["subject", "object"]] | None = None,
) -> tuple[list[Closure], list[Conflict]]:
    """Compute derived ends (and unordered pairs) for a fact set.

    ``sides`` lists the closure sides the relation's flags enable:
    ``"subject"`` closes functional relations per subject, ``"object"``
    mirrors it for inverse-functional relations per object. Both sides
    feed one shared successor map and a single diff pass, so a fact
    closed from both sides ends at the earliest successor either side
    sees — independent of arrival order. ``side`` is the single-side
    shorthand used when ``sides`` is not given. Invalidated and mood
    facts never participate.
    """
    active_sides: list[Literal["subject", "object"]] = list(sides) if sides is not None else [side]
    participating = [fact for fact in facts if fact.invalidated_at is None and fact.mood is None]

    conflicts: list[Conflict] = []
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    # fact id -> earliest later different-value successor across all sides.
    successors: dict[uuid.UUID, FactData] = {}

    for current_side in active_sides:
        groups: dict[tuple[str, str], list[FactData]] = {}
        for fact in participating:
            groups.setdefault(_group_key(fact, current_side), []).append(fact)

        for group in groups.values():
            unknown = [f for f in group if f.valid_from is None]
            known = sorted((f for f in group if f.valid_from is not None), key=_successor_key)

            for fact in unknown:
                for other in group:
                    if other.id == fact.id or _value_key(other, current_side) == _value_key(
                        fact, current_side
                    ):
                        continue
                    first, second = sorted((fact.id, other.id), key=str)
                    pair = (first, second)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    conflicts.append(
                        Conflict(
                            kind="unordered_succession",
                            predicate_key=fact.predicate_key,
                            subject_id=fact.subject_id if current_side == "subject" else None,
                            object_id=fact.object_id if current_side == "object" else None,
                            fact_ids=pair,
                            detail=(
                                "a successor with an unknown start closes nothing; "
                                "a person must order these two values"
                            ),
                        )
                    )

            for fact in known:
                candidates = [
                    other
                    for other in known
                    if _start(other) > _start(fact)
                    and _value_key(other, current_side) != _value_key(fact, current_side)
                ]
                if not candidates:
                    continue
                successor = min(candidates, key=_successor_key)
                existing = successors.get(fact.id)
                if existing is None or _successor_key(successor) < _successor_key(existing):
                    successors[fact.id] = successor

    closures: list[Closure] = []
    for fact in participating:
        if fact.valid_from is None:
            continue  # an unknown start is never closed or reopened here
        if fact.valid_to is not None and not fact.end_derived:
            continue  # an explicit end is never rewritten
        closing: FactData | None = successors.get(fact.id)
        if closing is None:
            # No side has a successor: a previously derived end is stale
            # (its successor was retracted) and reopens.
            if fact.end_derived and fact.valid_to is not None:
                closures.append(Closure(fact_id=fact.id, new_valid_to=WorldBound.unknown()))
            continue
        assert closing.valid_from is not None
        desired = ensure_utc(closing.valid_from)
        if (
            fact.valid_to is not None
            and fact.end_derived
            and ensure_utc(fact.valid_to) == desired
            and fact.valid_to_precision is closing.valid_from_precision
        ):
            continue  # already closed at exactly this instant and precision
        closures.append(
            Closure(
                fact_id=fact.id,
                new_valid_to=WorldBound(instant=desired, precision=closing.valid_from_precision),
            )
        )

    closures.sort(key=lambda c: str(c.fact_id))
    conflicts.sort(key=lambda c: tuple(str(i) for i in c.fact_ids))
    return closures, conflicts


__all__ = ["Closure", "compute_ends"]
