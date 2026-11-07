"""Forward-chaining reasoning over typed facts.

Pure module — no I/O. Four rules off the relation flags (symmetric,
inverse-of, sub-property-of, transitive) run to a fixed point with a sorted
frontier, at most ``MAX_DEPTH`` deep and ``MAX_PER_PREDICATE`` derived facts
per predicate. A derived fact's validity is the intersection of its
premises' intervals (empty means no derivation) and its confidence their
minimum. Asserted beats derived: a consequence identical to an asserted
fact is not emitted, and one contradicting an asserted functional value
becomes a violation, never a fact. Inputs are the asserted base; previously
derived facts are rebuilt from scratch by the service each run.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from forge_world.ledger import FactData, Layer, RelationDef, Statement, normalise_value
from forge_world.time import Precision, WorldBound, ensure_utc, intervals_overlap

MAX_DEPTH = 12
MAX_PER_PREDICATE = 20_000

_DERIVED_NAMESPACE = uuid.UUID("3f7a2c1e-9c4d-4e8f-b2a5-1d6e0a9f5c37")


@dataclass(frozen=True)
class Axioms:
    """The reasoning-relevant view of the declared relations."""

    by_key: Mapping[str, RelationDef] = field(default_factory=dict)

    @classmethod
    def from_relations(
        cls, relations: Axioms | Mapping[str, RelationDef] | Sequence[RelationDef]
    ) -> Axioms:
        if isinstance(relations, Axioms):
            return relations
        if isinstance(relations, Mapping):
            return cls(by_key=dict(relations))
        return cls(by_key={relation.key: relation for relation in relations})


@dataclass(frozen=True)
class DerivedFact:
    """One entailed fact (not yet persisted) with its proof.

    ``id`` is a deterministic synthetic id (uuid5 of the fact's identity) so
    premises can reference other derived facts before any of them is stored;
    the service maps synthetic ids to real row ids as it inserts in depth
    order.
    """

    id: uuid.UUID
    subject_id: uuid.UUID
    phrase: str
    predicate_key: str
    object_id: uuid.UUID | None
    value: str | None
    valid_from: WorldBound
    valid_to: WorldBound
    confidence: float
    rule: str
    premise_ids: tuple[uuid.UUID, ...]
    depth: int

    def to_statement(self) -> Statement:
        return Statement(
            subject_id=self.subject_id,
            phrase=self.phrase,
            predicate_key=self.predicate_key,
            object_id=self.object_id,
            value=self.value,
            layer=Layer.DERIVED,
            confidence=self.confidence,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
        )


@dataclass(frozen=True)
class Violation:
    """A consequence refused because an asserted fact contradicts it."""

    predicate_key: str
    subject_id: uuid.UUID
    object_id: uuid.UUID | None
    value: str | None
    asserted_fact_id: uuid.UUID
    rule: str
    premise_ids: tuple[uuid.UUID, ...]
    detail: str


@dataclass
class _Work:
    """Unified working-set entry (asserted row or derived candidate)."""

    id: uuid.UUID
    subject_id: uuid.UUID
    predicate_key: str
    object_id: uuid.UUID | None
    value: str | None
    valid_from: datetime | None
    valid_from_precision: Precision
    valid_to: datetime | None
    valid_to_precision: Precision
    confidence: float
    depth: int
    asserted: bool

    def target(self) -> tuple[str, str]:
        if self.object_id is not None:
            return ("entity", str(self.object_id))
        return ("value", normalise_value(self.value or ""))

    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            str(self.subject_id),
            self.predicate_key,
            "|".join(self.target()),
            "" if self.valid_from is None else ensure_utc(self.valid_from).isoformat(),
            "" if self.valid_to is None else ensure_utc(self.valid_to).isoformat(),
        )

    def key(
        self,
    ) -> tuple[uuid.UUID, str, tuple[str, uuid.UUID | str], datetime | None, datetime | None]:
        target: tuple[str, uuid.UUID | str] = (
            ("entity", self.object_id)
            if self.object_id is not None
            else ("value", normalise_value(self.value or ""))
        )
        return (
            self.subject_id,
            self.predicate_key,
            target,
            ensure_utc(self.valid_from) if self.valid_from is not None else None,
            ensure_utc(self.valid_to) if self.valid_to is not None else None,
        )


def _from_asserted(fact: FactData) -> _Work:
    assert fact.predicate_key is not None
    return _Work(
        id=fact.id,
        subject_id=fact.subject_id,
        predicate_key=fact.predicate_key,
        object_id=fact.object_id,
        value=fact.value,
        valid_from=ensure_utc(fact.valid_from) if fact.valid_from else None,
        valid_from_precision=fact.valid_from_precision,
        valid_to=ensure_utc(fact.valid_to) if fact.valid_to else None,
        valid_to_precision=fact.valid_to_precision,
        confidence=fact.confidence,
        depth=0,
        asserted=True,
    )


def _intersect(
    premises: Sequence[_Work],
) -> tuple[WorldBound, WorldBound] | None:
    """Interval intersection with precision provenance; None when empty."""
    froms: list[tuple[datetime, Precision]] = []
    tos: list[tuple[datetime, Precision]] = []
    for premise in premises:
        if premise.valid_from is not None:
            froms.append((ensure_utc(premise.valid_from), premise.valid_from_precision))
        if premise.valid_to is not None:
            tos.append((ensure_utc(premise.valid_to), premise.valid_to_precision))
    if froms:
        instant, precision = max(froms, key=lambda pair: pair[0])
        from_bound = WorldBound(instant=instant, precision=precision)
    else:
        from_bound = WorldBound.unknown()
    if tos:
        instant, precision = min(tos, key=lambda pair: pair[0])
        to_bound = WorldBound(instant=instant, precision=precision)
    else:
        to_bound = WorldBound.unknown()
    if (
        from_bound.instant is not None
        and to_bound.instant is not None
        and from_bound.instant >= to_bound.instant
    ):
        return None
    return from_bound, to_bound


@dataclass(frozen=True)
class _Candidate:
    subject_id: uuid.UUID
    predicate_key: str
    object_id: uuid.UUID | None
    value: str | None
    valid_from: WorldBound
    valid_to: WorldBound
    confidence: float
    rule: str
    premise_ids: tuple[uuid.UUID, ...]
    depth: int

    def identity(self) -> tuple[str, str, str, str, str]:
        target = (
            f"entity|{self.object_id}"
            if self.object_id is not None
            else f"value|{normalise_value(self.value or '')}"
        )
        return (
            str(self.subject_id),
            self.predicate_key,
            target,
            "" if self.valid_from.instant is None else self.valid_from.instant.isoformat(),
            "" if self.valid_to.instant is None else self.valid_to.instant.isoformat(),
        )

    def sort_key(self) -> tuple[str, ...]:
        return (*self.identity(), self.rule, ",".join(str(p) for p in self.premise_ids))

    def key(
        self,
    ) -> tuple[uuid.UUID, str, tuple[str, uuid.UUID | str], datetime | None, datetime | None]:
        target: tuple[str, uuid.UUID | str] = (
            ("entity", self.object_id)
            if self.object_id is not None
            else ("value", normalise_value(self.value or ""))
        )
        return (
            self.subject_id,
            self.predicate_key,
            target,
            ensure_utc(self.valid_from.instant) if self.valid_from.instant is not None else None,
            ensure_utc(self.valid_to.instant) if self.valid_to.instant is not None else None,
        )


def _candidate(
    *,
    subject_id: uuid.UUID,
    predicate_key: str,
    object_id: uuid.UUID | None,
    value: str | None,
    premises: Sequence[_Work],
    rule: str,
    max_depth: int,
) -> _Candidate | None:
    depth = max(p.depth for p in premises) + 1
    if depth > max_depth:
        return None
    bounds = _intersect(premises)
    if bounds is None:
        return None
    from_bound, to_bound = bounds
    return _Candidate(
        subject_id=subject_id,
        predicate_key=predicate_key,
        object_id=object_id,
        value=value,
        valid_from=from_bound,
        valid_to=to_bound,
        confidence=min(p.confidence for p in premises),
        rule=rule,
        premise_ids=tuple(p.id for p in premises),
        depth=depth,
    )


def _generate(
    work: list[_Work], frontier: list[_Work], axioms: Axioms, max_depth: int
) -> list[_Candidate]:
    """Candidates with at least one premise in ``frontier``.

    Single-premise rules fire from frontier entries only. Transitive
    fires frontier x work (frontier entry as first premise) and
    work x frontier (frontier entry as second premise, found through
    the by-object index); a pair of two frontier entries is generated
    twice and deduplicated by the caller.
    """
    candidates: list[_Candidate] = []
    by_subject: dict[tuple[str, uuid.UUID], list[_Work]] = {}
    by_object: dict[tuple[str, uuid.UUID], list[_Work]] = {}
    for entry in work:
        by_subject.setdefault((entry.predicate_key, entry.subject_id), []).append(entry)
        if entry.object_id is not None:
            by_object.setdefault((entry.predicate_key, entry.object_id), []).append(entry)

    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    def transitive(entry: _Work, nxt: _Work) -> None:
        if nxt.object_id is None:
            return
        pair = (entry.id, nxt.id)
        if pair in seen_pairs:  # both premises in frontier: generated once
            return
        seen_pairs.add(pair)
        made = _candidate(
            subject_id=entry.subject_id,
            predicate_key=entry.predicate_key,
            object_id=nxt.object_id,
            value=None,
            premises=(entry, nxt),
            rule="transitive",
            max_depth=max_depth,
        )
        if made is not None:
            candidates.append(made)

    for entry in frontier:
        relation = axioms.by_key.get(entry.predicate_key)
        if relation is None:
            continue
        if relation.sub_property_of and relation.sub_property_of in axioms.by_key:
            made = _candidate(
                subject_id=entry.subject_id,
                predicate_key=relation.sub_property_of,
                object_id=entry.object_id,
                value=entry.value,
                premises=(entry,),
                rule="sub_property",
                max_depth=max_depth,
            )
            if made is not None:
                candidates.append(made)
        if entry.object_id is None:
            continue  # the remaining rules need an entity object
        if relation.symmetric:
            made = _candidate(
                subject_id=entry.object_id,
                predicate_key=entry.predicate_key,
                object_id=entry.subject_id,
                value=None,
                premises=(entry,),
                rule="symmetric",
                max_depth=max_depth,
            )
            if made is not None:
                candidates.append(made)
        if relation.inverse_of and relation.inverse_of in axioms.by_key:
            made = _candidate(
                subject_id=entry.object_id,
                predicate_key=relation.inverse_of,
                object_id=entry.subject_id,
                value=None,
                premises=(entry,),
                rule="inverse",
                max_depth=max_depth,
            )
            if made is not None:
                candidates.append(made)
        if relation.transitive:
            for nxt in by_subject.get((entry.predicate_key, entry.object_id), []):
                transitive(entry, nxt)

    # Work x frontier: the frontier entry is the second premise.
    for nxt in frontier:
        relation = axioms.by_key.get(nxt.predicate_key)
        if relation is None or not relation.transitive:
            continue
        for entry in by_object.get((nxt.predicate_key, nxt.subject_id), []):
            transitive(entry, nxt)
    return candidates


def _contradiction(candidate: _Candidate, asserted: list[_Work], axioms: Axioms) -> _Work | None:
    """The asserted fact this candidate contradicts, if any (asserted wins)."""
    relation = axioms.by_key.get(candidate.predicate_key)
    if relation is None:
        return None
    for entry in asserted:
        if entry.predicate_key != candidate.predicate_key:
            continue
        candidate_target = (
            ("entity", str(candidate.object_id))
            if candidate.object_id is not None
            else ("value", normalise_value(candidate.value or ""))
        )
        if (
            relation.functional
            and entry.subject_id == candidate.subject_id
            and entry.target() != candidate_target
            and intervals_overlap(
                entry.valid_from,
                entry.valid_to,
                candidate.valid_from.instant,
                candidate.valid_to.instant,
            )
        ):
            return entry
        if (
            relation.inverse_functional
            and entry.target() == candidate_target
            and entry.subject_id != candidate.subject_id
            and intervals_overlap(
                entry.valid_from,
                entry.valid_to,
                candidate.valid_from.instant,
                candidate.valid_to.instant,
            )
        ):
            return entry
    return None


def derive(
    facts: Sequence[FactData],
    relations: Axioms | Mapping[str, RelationDef] | Sequence[RelationDef],
    *,
    max_depth: int = MAX_DEPTH,
    max_per_predicate: int = MAX_PER_PREDICATE,
) -> tuple[list[DerivedFact], list[Violation], list[str]]:
    """Forward-chain over the asserted ``facts`` to a fixed point.

    Returns ``(derived, violations, capped_predicates)``, derived sorted by
    depth so premises always precede their consequences. Mood and
    invalidated facts never participate; only typed (predicate-bound) facts
    are premises.
    """
    axioms = Axioms.from_relations(relations)
    work: list[_Work] = [
        _from_asserted(fact)
        for fact in facts
        if fact.invalidated_at is None and fact.mood is None and fact.predicate_key is not None
    ]
    asserted = list(work)
    known = {entry.key() for entry in work}
    derived: list[DerivedFact] = []
    violations: list[Violation] = []
    seen_violations: set[tuple[tuple[str, ...], uuid.UUID]] = set()
    capped: set[str] = set()
    per_predicate: dict[str, int] = {}

    frontier = list(work)
    while frontier:
        candidates = _generate(work, frontier, axioms, max_depth)
        # Drop already-known identities before sorting what remains.
        fresh: dict[
            tuple[uuid.UUID, str, tuple[str, uuid.UUID | str], datetime | None, datetime | None],
            _Candidate,
        ] = {}
        for candidate in sorted(
            (c for c in candidates if c.key() not in known), key=lambda c: c.sort_key()
        ):
            key = candidate.key()
            if key not in fresh:
                fresh[key] = candidate

        added = 0
        next_frontier: list[_Work] = []
        for candidate in sorted(fresh.values(), key=lambda c: c.sort_key()):
            blocker = _contradiction(candidate, asserted, axioms)
            if blocker is not None:
                violation_key = (candidate.identity(), blocker.id)
                if violation_key not in seen_violations:
                    seen_violations.add(violation_key)
                    violations.append(
                        Violation(
                            predicate_key=candidate.predicate_key,
                            subject_id=candidate.subject_id,
                            object_id=candidate.object_id,
                            value=candidate.value,
                            asserted_fact_id=blocker.id,
                            rule=candidate.rule,
                            premise_ids=candidate.premise_ids,
                            detail=(
                                f"derived {candidate.predicate_key!r} value contradicts "
                                "an asserted functional value; asserted wins"
                            ),
                        )
                    )
                continue
            if per_predicate.get(candidate.predicate_key, 0) >= max_per_predicate:
                capped.add(candidate.predicate_key)
                continue
            relation = axioms.by_key.get(candidate.predicate_key)
            synthetic = uuid.uuid5(_DERIVED_NAMESPACE, "|".join(candidate.identity()))
            fact = DerivedFact(
                id=synthetic,
                subject_id=candidate.subject_id,
                phrase=relation.label if relation is not None else candidate.predicate_key,
                predicate_key=candidate.predicate_key,
                object_id=candidate.object_id,
                value=candidate.value,
                valid_from=candidate.valid_from,
                valid_to=candidate.valid_to,
                confidence=candidate.confidence,
                rule=candidate.rule,
                premise_ids=candidate.premise_ids,
                depth=candidate.depth,
            )
            derived.append(fact)
            per_predicate[candidate.predicate_key] = (
                per_predicate.get(candidate.predicate_key, 0) + 1
            )
            new_entry = _Work(
                id=synthetic,
                subject_id=candidate.subject_id,
                predicate_key=candidate.predicate_key,
                object_id=candidate.object_id,
                value=candidate.value,
                valid_from=candidate.valid_from.instant,
                valid_from_precision=candidate.valid_from.precision,
                valid_to=candidate.valid_to.instant,
                valid_to_precision=candidate.valid_to.precision,
                confidence=candidate.confidence,
                depth=candidate.depth,
                asserted=False,
            )
            work.append(new_entry)
            next_frontier.append(new_entry)
            known.add(candidate.key())
            added += 1
        if added == 0:
            break
        frontier = next_frontier

    derived.sort(key=lambda d: (d.depth, d.predicate_key, str(d.subject_id), str(d.id)))
    violations.sort(key=lambda v: (v.predicate_key, str(v.subject_id), str(v.asserted_fact_id)))
    return derived, violations, sorted(capped)


__all__ = [
    "MAX_DEPTH",
    "MAX_PER_PREDICATE",
    "Axioms",
    "DerivedFact",
    "Violation",
    "derive",
]
