"""World-model orchestration: transactions over the pure modules + SQL.

``WorldModelService`` is the only place domain rules meet persistence: it
resolves names to entities (recall channel 1), asserts/supersedes/retracts
facts, runs temporal closing and reasoning, drives extraction through the
workspace ``ModelClient`` (refusing when none is configured — never a fake),
and runs the proposal lifecycle with its shadow gate. Methods flush but
never commit; the caller (HTTP router) owns the transaction, so a mutation
and its audit event commit atomically.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from forge_contracts import PROVIDER_FAILURE_STOP_REASONS, ModelClient
from forge_knowledge.chunking import chunk_markdown
from forge_world.conflicts import Conflict, functional_conflicts
from forge_world.decisions import DriftReport, drift
from forge_world.extraction import DropReason, build_request, parse_reply, verify
from forge_world.governance import Precedent, Verdict, gate
from forge_world.identity import classes_disjoint, names_match, normalise_name, types_compatible
from forge_world.ledger import (
    KNOWN_AS_KEY,
    KNOWN_AS_RELATION,
    ClassDef,
    ConflictError,
    DecisionData,
    EntityData,
    EvidenceData,
    FactData,
    FeedbackData,
    FeedbackOutcome,
    ForbiddenError,
    InvalidatedReason,
    Layer,
    MergeData,
    ModelNotConfiguredError,
    Mood,
    NotFoundError,
    ProposalData,
    ProposalStatus,
    RelationDef,
    RelationKind,
    SourceKind,
    Statement,
    WorldValidationError,
    dedup_key,
    effective_confidence,
    normalise_phrase,
    normalise_value,
    validate_key,
)
from forge_world.reasoning import Violation
from forge_world.reasoning import derive as _derive_pure
from forge_world.repository import WorldRepository
from forge_world.temporal import Closure, compute_ends
from forge_world.time import WorldBound, ensure_utc, parse_bound, period_end

Clock = Callable[[], datetime]

#: How far a merge chain is followed when resolving an id to its survivor.
_MAX_MERGE_HOPS = 8


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StatementInput:
    """A claim as a caller states it (entities by id or by name)."""

    phrase: str
    subject_id: uuid.UUID | None = None
    subject_name: str | None = None
    subject_type: str | None = None
    predicate_key: str | None = None
    object_id: uuid.UUID | None = None
    object_name: str | None = None
    object_type: str | None = None
    value: str | None = None
    layer: Layer | None = None
    mood: Mood | None = None
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    valid_from_text: str | None = None
    valid_to_text: str | None = None

    def identity(self) -> str:
        """Normalised comparable identity (governance precedents)."""
        subject = (
            str(self.subject_id)
            if self.subject_id is not None
            else normalise_name(self.subject_name or "")
        )
        if self.object_id is not None:
            target = str(self.object_id)
        elif self.object_name is not None:
            target = normalise_name(self.object_name)
        else:
            target = normalise_value(self.value or "")
        return "|".join(
            (
                subject,
                self.predicate_key or normalise_phrase(self.phrase),
                target,
                (self.valid_from_text or "").strip(),
                (self.valid_to_text or "").strip(),
            )
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "subject_id": str(self.subject_id) if self.subject_id else None,
            "subject_name": self.subject_name,
            "subject_type": self.subject_type,
            "predicate_key": self.predicate_key,
            "object_id": str(self.object_id) if self.object_id else None,
            "object_name": self.object_name,
            "object_type": self.object_type,
            "value": self.value,
            "layer": self.layer.value if self.layer else None,
            "mood": self.mood.value if self.mood else None,
            "qualifiers": dict(self.qualifiers),
            "confidence": self.confidence,
            "valid_from": self.valid_from_text,
            "valid_to": self.valid_to_text,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> StatementInput:
        def _uuid(key: str) -> uuid.UUID | None:
            raw = payload.get(key)
            return uuid.UUID(str(raw)) if raw else None

        layer = payload.get("layer")
        mood = payload.get("mood")
        return cls(
            phrase=str(payload.get("phrase") or ""),
            subject_id=_uuid("subject_id"),
            subject_name=payload.get("subject_name"),
            subject_type=payload.get("subject_type"),
            predicate_key=payload.get("predicate_key"),
            object_id=_uuid("object_id"),
            object_name=payload.get("object_name"),
            object_type=payload.get("object_type"),
            value=payload.get("value"),
            layer=Layer(str(layer)) if layer else None,
            mood=Mood(str(mood)) if mood else None,
            qualifiers=dict(payload.get("qualifiers") or {}),
            confidence=float(payload.get("confidence") or 1.0),
            valid_from_text=payload.get("valid_from"),
            valid_to_text=payload.get("valid_to"),
        )


@dataclass(frozen=True)
class AssertResult:
    fact: FactData
    created: bool
    created_entity_ids: tuple[uuid.UUID, ...] = ()
    typed_entity_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class IngestResult:
    chunks: int
    facts_created: int
    duplicates: int
    entities_created: int
    drops: int
    drops_by_reason: dict[str, int]
    fact_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class DeriveResult:
    created: int
    invalidated: int
    capped: tuple[str, ...]
    violations: tuple[Violation, ...]


@dataclass(frozen=True)
class EntityProfile:
    entity: EntityData
    resolved_id: uuid.UUID
    names: tuple[str, ...]
    facts: tuple[FactData, ...]
    object_of: tuple[FactData, ...]


@dataclass(frozen=True)
class ChangeEvent:
    kind: Literal["asserted", "invalidated"]
    fact_id: uuid.UUID
    occurred_at: datetime
    reason: str | None = None


@dataclass(frozen=True)
class ProposalDecisionOutcome:
    proposal: ProposalData
    fact: FactData | None
    result: AssertResult | None = None


class WorldModelService:
    """Orchestrates the pure domain modules over the repository."""

    def __init__(
        self,
        repo: WorldRepository,
        model_client: ModelClient | None = None,
        clock: Clock | None = None,
        *,
        model: str = "",
    ) -> None:
        self._repo = repo
        self._model_client = model_client
        self._clock = clock or _utc_now
        self._model = model

    @property
    def session(self) -> Session:
        return self._repo.session

    @property
    def repository(self) -> WorldRepository:
        return self._repo

    def _now(self) -> datetime:
        return ensure_utc(self._clock())

    # --- ontology ------------------------------------------------------ #

    def create_class(
        self,
        workspace_id: uuid.UUID,
        *,
        key: str,
        label: str,
        parent_key: str | None = None,
        disjoint_with: Sequence[str] = (),
        iri: str | None = None,
    ) -> ClassDef:
        validate_key(key, what="class key")
        if self._repo.get_class(workspace_id, key) is not None:
            raise ConflictError(f"class {key!r} already exists")
        if parent_key is not None:
            validate_key(parent_key, what="parent key")
            if parent_key == key:
                raise WorldValidationError(f"class {key!r} lists itself as parent")
            if self._repo.get_class(workspace_id, parent_key) is None:
                raise WorldValidationError(f"unknown parent class {parent_key!r}")
        for other in disjoint_with:
            validate_key(other, what="disjoint class key")
            if other != key and self._repo.get_class(workspace_id, other) is None:
                raise WorldValidationError(f"unknown disjoint class {other!r}")
        return self._repo.insert_class(
            workspace_id,
            ClassDef(
                key=key,
                label=label,
                parent_key=parent_key,
                disjoint_with=tuple(disjoint_with),
                iri=iri,
            ),
            now=self._now(),
        )

    def list_classes(self, workspace_id: uuid.UUID) -> list[ClassDef]:
        return self._repo.list_classes(workspace_id)

    def _classes_map(self, workspace_id: uuid.UUID) -> dict[str, ClassDef]:
        return {entry.key: entry for entry in self._repo.list_classes(workspace_id)}

    def create_relation(
        self,
        workspace_id: uuid.UUID,
        *,
        key: str,
        label: str,
        kind: RelationKind,
        domain_class: str | None = None,
        range_class: str | None = None,
        functional: bool = False,
        inverse_functional: bool = False,
        transitive: bool = False,
        symmetric: bool = False,
        inverse_of: str | None = None,
        sub_property_of: str | None = None,
        iri: str | None = None,
    ) -> RelationDef:
        validate_key(key, what="relation key")
        if self.get_relation(workspace_id, key) is not None:
            raise ConflictError(f"relation {key!r} already exists")
        classes = self._classes_map(workspace_id)
        if domain_class is not None and domain_class not in classes:
            raise WorldValidationError(f"unknown domain class {domain_class!r}")
        if range_class is not None and range_class != "value" and range_class not in classes:
            raise WorldValidationError(f"unknown range class {range_class!r}")
        for ref, what in ((inverse_of, "inverse_of"), (sub_property_of, "sub_property_of")):
            if ref is not None and ref != KNOWN_AS_KEY:
                validate_key(ref, what=f"{what} key")
                if self._repo.get_relation(workspace_id, ref) is None:
                    raise WorldValidationError(f"unknown relation key {ref!r} for {what}")
        return self._repo.insert_relation(
            workspace_id,
            RelationDef(
                key=key,
                label=label,
                kind=kind,
                domain_class=domain_class,
                range_class=range_class,
                functional=functional,
                inverse_functional=inverse_functional,
                transitive=transitive,
                symmetric=symmetric,
                inverse_of=inverse_of,
                sub_property_of=sub_property_of,
                iri=iri,
            ),
            now=self._now(),
        )

    def get_relation(self, workspace_id: uuid.UUID, key: str) -> RelationDef | None:
        row = self._repo.get_relation(workspace_id, key)
        if row is not None:
            return row
        return KNOWN_AS_RELATION if key == KNOWN_AS_KEY else None

    def list_relations(self, workspace_id: uuid.UUID) -> list[RelationDef]:
        rows = self._repo.list_relations(workspace_id)
        if all(row.key != KNOWN_AS_KEY for row in rows):
            rows = sorted([*rows, KNOWN_AS_RELATION], key=lambda row: row.key)
        return rows

    def _relations_map(self, workspace_id: uuid.UUID) -> dict[str, RelationDef]:
        return {row.key: row for row in self.list_relations(workspace_id)}

    # --- statement plumbing -------------------------------------------- #

    def _entity_or_404(self, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> EntityData:
        entity = self._repo.get_entity(workspace_id, entity_id)
        if entity is None:
            raise NotFoundError(f"no entity {entity_id}")
        return entity

    def _names_by_entity(
        self, workspace_id: uuid.UUID, *, now: datetime
    ) -> dict[uuid.UUID, list[str]]:
        names: dict[uuid.UUID, list[str]] = {}
        for fact in self._repo.query_facts(
            workspace_id, at=now, as_of=now, predicate_key=KNOWN_AS_KEY
        ):
            if fact.value:
                names.setdefault(fact.subject_id, []).append(fact.value)
        return names

    def _recall(
        self,
        workspace_id: uuid.UUID,
        name: str,
        *,
        type_key: str | None,
        now: datetime,
    ) -> EntityData | None:
        """Channel-1 recall: type-compatible name match, exact before suffix.

        Two passes over the earliest-created-first candidates: an exact
        normalised match always wins, even over an older entity that only
        matches after corporate-suffix stripping.
        """
        classes = self._classes_map(workspace_id)
        names = self._names_by_entity(workspace_id, now=now)
        compatible: list[tuple[EntityData, list[str]]] = []
        for entity in self._repo.list_entities(workspace_id):
            if entity.described:
                continue  # described things are never recalled
            if not types_compatible(entity.type_key, type_key, classes):
                continue
            compatible.append((entity, [entity.label, *names.get(entity.id, [])]))
        target = normalise_name(name)
        if target:
            for entity, candidate_names in compatible:
                if any(
                    candidate.strip() and normalise_name(candidate) == target
                    for candidate in candidate_names
                ):
                    return entity
        for entity, candidate_names in compatible:
            if any(names_match(name, candidate) for candidate in candidate_names):
                return entity
        return None

    def _assert_known_as(
        self,
        workspace_id: uuid.UUID,
        entity: EntityData,
        name: str,
        *,
        source_kind: SourceKind,
        actor: str,
        proposal_id: uuid.UUID | None,
        evidence: tuple[str, int, str, int, int, datetime | None] | None,
        now: datetime,
    ) -> None:
        existing = {
            normalise_name(value)
            for value in self._names_by_entity(workspace_id, now=now).get(entity.id, [])
        }
        if normalise_name(name) in existing:
            return
        statement = Statement(
            subject_id=entity.id,
            phrase=KNOWN_AS_RELATION.label,
            predicate_key=KNOWN_AS_KEY,
            object_id=None,
            value=name,
            layer=Layer.TYPED,
        )
        fact, _created = self._repo.insert_fact(
            workspace_id,
            statement,
            source_kind=source_kind,
            actor=actor,
            proposal_id=proposal_id,
            now=now,
        )
        if evidence is not None:
            source_ref, chunk_index, quote, start, end, doc_time = evidence
            self._repo.insert_evidence(
                workspace_id,
                fact_id=fact.id,
                source_ref=source_ref,
                chunk_index=chunk_index,
                quote=quote,
                start_offset=start,
                end_offset=end,
                doc_time=doc_time,
                now=now,
            )

    def _recall_or_create(
        self,
        workspace_id: uuid.UUID,
        name: str,
        *,
        type_key: str | None,
        source_kind: SourceKind,
        actor: str,
        proposal_id: uuid.UUID | None,
        evidence: tuple[str, int, str, int, int, datetime | None] | None,
        now: datetime,
    ) -> tuple[EntityData, bool, bool]:
        entity = self._recall(workspace_id, name, type_key=type_key, now=now)
        created = False
        typed = False
        if entity is None:
            entity = self._repo.insert_entity(
                workspace_id,
                label=name,
                type_key=type_key,
                type_source=(
                    ("person" if source_kind is SourceKind.PERSON else "model")
                    if type_key is not None
                    else None
                ),
                now=now,
            )
            created = True
            typed = type_key is not None
        elif type_key is not None and entity.type_key is None:
            entity = self._repo.set_entity_type(
                workspace_id,
                entity.id,
                type_key=type_key,
                source="person" if source_kind is SourceKind.PERSON else "model",
                now=now,
            )
            typed = True
        self._assert_known_as(
            workspace_id,
            entity,
            name,
            source_kind=source_kind,
            actor=actor,
            proposal_id=proposal_id,
            evidence=evidence,
            now=now,
        )
        return entity, created, typed

    def _adjust_for_relation(self, statement: Statement, relation: RelationDef | None) -> Statement:
        if relation is None:
            return statement
        if relation.kind is RelationKind.EVENT and statement.valid_from.instant is not None:
            start = statement.valid_from
            start_instant = start.instant
            assert start_instant is not None
            return replace(
                statement,
                valid_to=WorldBound(
                    instant=period_end(start_instant, start.precision),
                    precision=start.precision,
                    end_after_period=True,
                ),
                end_derived=False,
            )
        if relation.kind is RelationKind.ETERNAL:
            return replace(
                statement,
                valid_from=WorldBound.unknown(),
                valid_to=WorldBound.unknown(),
                end_derived=False,
            )
        return statement

    def _validate_statement(
        self, workspace_id: uuid.UUID, statement: Statement
    ) -> tuple[Statement, RelationDef | None]:
        if statement.layer is Layer.DERIVED:
            raise WorldValidationError("derived facts are written only by reasoning")
        relation: RelationDef | None = None
        if statement.predicate_key is not None:
            relation = self.get_relation(workspace_id, statement.predicate_key)
            if relation is None:
                raise WorldValidationError(f"unknown relation key {statement.predicate_key!r}")
            if statement.layer is Layer.OPEN:
                statement = replace(statement, layer=Layer.TYPED)
            if relation.range_class == "value" and statement.object_id is not None:
                raise WorldValidationError(
                    f"relation {relation.key!r} takes a literal value, not an entity"
                )
            if relation.range_class not in (None, "value") and statement.value is not None:
                raise WorldValidationError(
                    f"relation {relation.key!r} takes an entity, not a literal value"
                )
            start_bound, end_bound = statement.valid_from, statement.valid_to
            if (
                relation.kind is RelationKind.STATE
                and start_bound.instant is not None
                and end_bound.instant is not None
                and end_bound.end_after_period
                and end_bound.precision == start_bound.precision
                and end_bound.instant == period_end(start_bound.instant, start_bound.precision)
            ):
                # Equal stated ends ("2024-06-01" to "2024-06-01") parse to
                # exactly the start's own period — not a stated span.
                raise WorldValidationError("a state span needs valid_to after valid_from")
        elif statement.layer is Layer.TYPED:
            raise WorldValidationError("a typed fact needs a predicate_key")
        self._entity_or_404(workspace_id, statement.subject_id)
        if statement.object_id is not None:
            self._entity_or_404(workspace_id, statement.object_id)
        return self._adjust_for_relation(statement, relation), relation

    def _parse_input_bound(self, text: str | None, *, is_end: bool) -> WorldBound:
        try:
            return parse_bound(text, is_end=is_end)
        except ValueError as exc:
            raise WorldValidationError(str(exc)) from exc

    def _check_type(self, workspace_id: uuid.UUID, type_key: str | None) -> None:
        if type_key is None:
            return
        validate_key(type_key, what="entity type key")
        if self._repo.get_class(workspace_id, type_key) is None:
            raise WorldValidationError(f"unknown entity type {type_key!r}")

    def _resolve_input(
        self,
        workspace_id: uuid.UUID,
        inp: StatementInput,
        *,
        source_kind: SourceKind,
        actor: str,
        proposal_id: uuid.UUID | None,
        now: datetime,
    ) -> tuple[Statement, tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
        self._check_type(workspace_id, inp.subject_type)
        self._check_type(workspace_id, inp.object_type)
        created_ids: list[uuid.UUID] = []
        typed_ids: list[uuid.UUID] = []
        if (inp.subject_id is None) == (inp.subject_name is None):
            raise WorldValidationError("give exactly one of subject_id and subject_name")
        targets = sum(
            candidate is not None for candidate in (inp.object_id, inp.object_name, inp.value)
        )
        if targets != 1:
            raise WorldValidationError("give exactly one of object_id, object_name and value")
        if inp.subject_id is not None:
            subject_id = self._entity_or_404(workspace_id, inp.subject_id).id
        else:
            assert inp.subject_name is not None
            subject, made, typed = self._recall_or_create(
                workspace_id,
                inp.subject_name,
                type_key=inp.subject_type,
                source_kind=source_kind,
                actor=actor,
                proposal_id=proposal_id,
                evidence=None,
                now=now,
            )
            if made:
                created_ids.append(subject.id)
            if typed:
                typed_ids.append(subject.id)
            subject_id = subject.id
        object_id: uuid.UUID | None = None
        if inp.object_id is not None:
            object_id = self._entity_or_404(workspace_id, inp.object_id).id
        elif inp.object_name is not None:
            found, made, typed = self._recall_or_create(
                workspace_id,
                inp.object_name,
                type_key=inp.object_type,
                source_kind=source_kind,
                actor=actor,
                proposal_id=proposal_id,
                evidence=None,
                now=now,
            )
            if made:
                created_ids.append(found.id)
            if typed:
                typed_ids.append(found.id)
            object_id = found.id
        layer = inp.layer or (Layer.TYPED if inp.predicate_key else Layer.OPEN)
        statement = Statement(
            subject_id=subject_id,
            phrase=inp.phrase,
            predicate_key=inp.predicate_key,
            object_id=object_id,
            value=inp.value,
            layer=layer,
            mood=inp.mood,
            qualifiers=dict(inp.qualifiers),
            confidence=inp.confidence,
            valid_from=self._parse_input_bound(inp.valid_from_text, is_end=False),
            valid_to=self._parse_input_bound(inp.valid_to_text, is_end=True),
        )
        return statement, tuple(created_ids), tuple(typed_ids)

    # --- writes -------------------------------------------------------- #

    def _closed_form(
        self,
        workspace_id: uuid.UUID,
        statement: Statement,
        relation: RelationDef | None,
        now: datetime,
    ) -> Statement:
        """The candidate as temporal closing would leave it, pre-insert.

        Dedup keys on the stored bounds, so re-asserting a fact whose
        live copy was closed must present the *closed* statement to
        ``insert_fact`` — otherwise the open-ended insert succeeds and
        closing tombstones it in the same transaction, leaving an
        orphan superseded row on every repeat.
        """
        if relation is None or relation.kind is not RelationKind.STATE:
            return statement
        if statement.layer is Layer.DERIVED:
            return statement
        if not (relation.functional or relation.inverse_functional):
            return statement
        sides: list[Literal["subject", "object"]] = []
        if relation.functional:
            sides.append("subject")
        if relation.inverse_functional:
            sides.append("object")
        provisional = FactData(
            id=uuid.uuid4(),
            subject_id=statement.subject_id,
            phrase=statement.phrase,
            predicate_key=statement.predicate_key,
            object_id=statement.object_id,
            value=statement.value,
            layer=statement.layer,
            mood=statement.mood,
            qualifiers=dict(statement.qualifiers),
            confidence=statement.confidence,
            valid_from=statement.valid_from.instant,
            valid_from_precision=statement.valid_from.precision,
            valid_to=statement.valid_to.instant,
            valid_to_precision=statement.valid_to.precision,
            end_derived=statement.end_derived,
            end_after_period=statement.valid_to.end_after_period,
            source_kind=SourceKind.PERSON,
            actor="",
            proposal_id=None,
            supersedes_id=None,
            dedup_key=dedup_key(statement),
            recorded_at=now,
            invalidated_at=None,
            invalidated_reason=None,
        )
        live = [
            fact
            for fact in self._repo.group_facts(workspace_id, as_of=now, predicate_key=relation.key)
            if fact.layer is not Layer.DERIVED
        ]
        closures, _ = compute_ends([*live, provisional], sides=sides)
        for closure in closures:
            if closure.fact_id == provisional.id and not closure.new_valid_to.is_unknown:
                return replace(statement, valid_to=closure.new_valid_to, end_derived=True)
        return statement

    def assert_statement(
        self,
        workspace_id: uuid.UUID,
        statement: Statement,
        *,
        source_kind: SourceKind,
        actor: str,
        proposal_id: uuid.UUID | None = None,
    ) -> AssertResult:
        """Assert one resolved statement (dedup returns the live fact)."""
        now = self._now()
        adjusted, relation = self._validate_statement(workspace_id, statement)
        adjusted = self._closed_form(workspace_id, adjusted, relation, now)
        fact, created = self._repo.insert_fact(
            workspace_id,
            adjusted,
            source_kind=source_kind,
            actor=actor,
            proposal_id=proposal_id,
            now=now,
        )
        if created:
            self._close_after(workspace_id, relation, now=now)
            fact = self._live_version(workspace_id, fact)
        return AssertResult(fact=fact, created=created)

    def assert_input(
        self,
        workspace_id: uuid.UUID,
        inp: StatementInput,
        *,
        source_kind: SourceKind,
        actor: str,
        proposal_id: uuid.UUID | None = None,
    ) -> AssertResult:
        """Resolve a caller-stated claim (names allowed) and assert it."""
        now = self._now()
        statement, created_ids, typed_ids = self._resolve_input(
            workspace_id,
            inp,
            source_kind=source_kind,
            actor=actor,
            proposal_id=proposal_id,
            now=now,
        )
        adjusted, relation = self._validate_statement(workspace_id, statement)
        adjusted = self._closed_form(workspace_id, adjusted, relation, now)
        fact, created = self._repo.insert_fact(
            workspace_id,
            adjusted,
            source_kind=source_kind,
            actor=actor,
            proposal_id=proposal_id,
            now=now,
        )
        if created:
            self._close_after(workspace_id, relation, now=now)
            fact = self._live_version(workspace_id, fact)
        return AssertResult(
            fact=fact,
            created=created,
            created_entity_ids=created_ids,
            typed_entity_ids=typed_ids,
        )

    def supersede(
        self,
        workspace_id: uuid.UUID,
        fact_id: uuid.UUID,
        inp: StatementInput,
        *,
        source_kind: SourceKind,
        actor: str,
    ) -> AssertResult:
        """Correct a fact: invalidate the old row, then insert its successor."""
        now = self._now()
        old = self._repo.get_fact(workspace_id, fact_id)
        if old is None:
            raise NotFoundError(f"no fact {fact_id}")
        if old.invalidated_at is not None:
            raise ConflictError(f"fact {fact_id} is already invalidated")
        statement, created_ids, typed_ids = self._resolve_input(
            workspace_id,
            inp,
            source_kind=source_kind,
            actor=actor,
            proposal_id=None,
            now=now,
        )
        adjusted, relation = self._validate_statement(workspace_id, statement)
        # Invalidate first, then insert (one transaction, in that order), so
        # the live-dedup index never sees both rows as live duplicates.
        self._repo.invalidate_fact(
            workspace_id, fact_id, reason=InvalidatedReason.SUPERSEDED, now=now
        )
        adjusted = self._closed_form(workspace_id, adjusted, relation, now)
        fact, created = self._repo.insert_fact(
            workspace_id,
            adjusted,
            source_kind=source_kind,
            actor=actor,
            supersedes_id=fact_id,
            now=now,
        )
        if not created:
            raise ConflictError(
                f"correction of {fact_id} duplicates live fact {fact.id}; retract instead"
            )
        old_relation = (
            self.get_relation(workspace_id, old.predicate_key)
            if old.predicate_key is not None
            else None
        )
        self._close_after(workspace_id, relation, now=now)
        if old_relation is not None and (relation is None or old_relation.key != relation.key):
            self._close_after(workspace_id, old_relation, now=now)
        return AssertResult(
            fact=self._live_version(workspace_id, fact),
            created=created,
            created_entity_ids=created_ids,
            typed_entity_ids=typed_ids,
        )

    def retract(self, workspace_id: uuid.UUID, fact_id: uuid.UUID, *, actor: str) -> FactData:
        """Invalidate a fact as retracted; derived ends recompute after it."""
        now = self._now()
        old = self._repo.get_fact(workspace_id, fact_id)
        if old is None:
            raise NotFoundError(f"no fact {fact_id}")
        result = self._repo.invalidate_fact(
            workspace_id, fact_id, reason=InvalidatedReason.RETRACTED, now=now
        )
        relation = (
            self.get_relation(workspace_id, old.predicate_key)
            if old.predicate_key is not None
            else None
        )
        self._close_after(workspace_id, relation, now=now)
        return result

    # --- temporal closing application ----------------------------------- #

    def _close_after(
        self, workspace_id: uuid.UUID, relation: RelationDef | None, *, now: datetime
    ) -> None:
        """Recompute derived ends for one functional state relation."""
        if relation is None or relation.kind is not RelationKind.STATE:
            return
        if not (relation.functional or relation.inverse_functional):
            return
        facts = [
            fact
            for fact in self._repo.group_facts(workspace_id, as_of=now, predicate_key=relation.key)
            if fact.layer is not Layer.DERIVED
        ]
        sides: list[Literal["subject", "object"]] = []
        if relation.functional:
            sides.append("subject")
        if relation.inverse_functional:
            sides.append("object")
        closures, _ = compute_ends(facts, sides=sides)
        self._apply_closures(workspace_id, closures, facts, now=now)

    def _apply_closures(
        self,
        workspace_id: uuid.UUID,
        closures: Sequence[Closure],
        facts: Sequence[FactData],
        *,
        now: datetime,
    ) -> None:
        by_id = {fact.id: fact for fact in facts}
        for closure in closures:
            old = by_id.get(closure.fact_id)
            if old is None or old.invalidated_at is not None:
                continue
            current = self._repo.get_fact(workspace_id, old.id)
            if current is None or current.invalidated_at is not None:
                continue
            statement = replace(
                current.to_statement(),
                valid_to=closure.new_valid_to,
                end_derived=not closure.new_valid_to.is_unknown,
            )
            self._repo.invalidate_fact(
                workspace_id,
                current.id,
                reason=InvalidatedReason.SUPERSEDED,
                now=now,
            )
            self._repo.insert_fact(
                workspace_id,
                statement,
                source_kind=current.source_kind,
                actor=current.actor,
                proposal_id=current.proposal_id,
                supersedes_id=current.id,
                now=now,
            )

    # --- merges and resolution ------------------------------------------ #

    def _merge_map(self, workspace_id: uuid.UUID, *, as_of: datetime) -> dict[uuid.UUID, uuid.UUID]:
        return {
            merge.merged_id: merge.survivor_id
            for merge in self._repo.list_merges(workspace_id, as_of=as_of)
        }

    @staticmethod
    def _resolve_id(entity_id: uuid.UUID, merge_map: Mapping[uuid.UUID, uuid.UUID]) -> uuid.UUID:
        current = entity_id
        seen = {current}
        for _ in range(_MAX_MERGE_HOPS):
            nxt = merge_map.get(current)
            if nxt is None or nxt in seen:
                break
            seen.add(nxt)
            current = nxt
        return current

    def _closure_ids(
        self, resolved_id: uuid.UUID, merge_map: Mapping[uuid.UUID, uuid.UUID]
    ) -> list[uuid.UUID]:
        ids = {resolved_id}
        for merged_id in merge_map:
            if self._resolve_id(merged_id, merge_map) == resolved_id:
                ids.add(merged_id)
        return sorted(ids, key=str)

    def merge_entities(
        self,
        workspace_id: uuid.UUID,
        *,
        survivor_id: uuid.UUID,
        merged_id: uuid.UUID,
        actor: str,
        reason: str = "",
    ) -> MergeData:
        now = self._now()
        if survivor_id == merged_id:
            raise ConflictError("an entity cannot be merged into itself")
        self._entity_or_404(workspace_id, survivor_id)
        self._entity_or_404(workspace_id, merged_id)
        merge_map = self._merge_map(workspace_id, as_of=now)
        if merged_id in merge_map:
            raise ConflictError(f"entity {merged_id} already has a live merge")
        if survivor_id in merge_map:
            raise ConflictError(
                f"survivor {survivor_id} is itself merged into "
                f"{self._resolve_id(survivor_id, merge_map)}"
            )
        if self._resolve_id(merged_id, merge_map) == self._resolve_id(survivor_id, merge_map):
            raise ConflictError("both ids already resolve to the same entity")
        classes = self._classes_map(workspace_id)

        def _closure_types(resolved: uuid.UUID) -> set[str]:
            types: set[str] = set()
            for entity_id in self._closure_ids(resolved, merge_map):
                entity = self._repo.get_entity(workspace_id, entity_id)
                if entity is not None and entity.type_key is not None:
                    types.add(entity.type_key)
            return types

        survivor_types = _closure_types(self._resolve_id(survivor_id, merge_map))
        merged_types = _closure_types(self._resolve_id(merged_id, merge_map))
        for survivor_type in sorted(survivor_types):
            for merged_type in sorted(merged_types):
                if classes_disjoint(survivor_type, merged_type, classes):
                    raise ConflictError(
                        f"classes {survivor_type!r} and {merged_type!r} are disjoint"
                    )
        return self._repo.insert_merge(
            workspace_id,
            survivor_id=survivor_id,
            merged_id=merged_id,
            actor=actor,
            reason=reason,
            now=now,
        )

    def revert_merge(
        self,
        workspace_id: uuid.UUID,
        merge_id: uuid.UUID,
        *,
        actor: str,
        reason: str | None = None,
    ) -> MergeData:
        return self._repo.revert_merge(
            workspace_id,
            merge_id,
            reverted_by=actor,
            reason=reason,
            now=self._now(),
        )

    def _live_version(self, workspace_id: uuid.UUID, fact: FactData) -> FactData:
        """Follow supersession (e.g. a just-closed fact) to its live copy."""
        current = self._repo.get_fact(workspace_id, fact.id) or fact
        for _ in range(16):
            if current.invalidated_at is None:
                return current
            if current.invalidated_reason is not InvalidatedReason.SUPERSEDED:
                return current
            successor_id = self._repo.successors(workspace_id, [current.id]).get(current.id)
            if successor_id is None:
                return current
            successor = self._repo.get_fact(workspace_id, successor_id)
            if successor is None:
                return current
            current = successor
        return current

    # --- reads ---------------------------------------------------------- #

    def get_fact(self, workspace_id: uuid.UUID, fact_id: uuid.UUID) -> FactData:
        fact = self._repo.get_fact(workspace_id, fact_id)
        if fact is None:
            raise NotFoundError(f"no fact {fact_id}")
        return fact

    def query_facts(
        self,
        workspace_id: uuid.UUID,
        *,
        subject: uuid.UUID | None = None,
        predicate: str | None = None,
        object_id: uuid.UUID | None = None,
        layer: Layer | None = None,
        at: datetime | None = None,
        as_of: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[FactData]:
        """Facts holding at ``at`` as known at ``as_of`` (merge-aware)."""
        at_moment = ensure_utc(at) if at is not None else self._now()
        as_of_moment = ensure_utc(as_of) if as_of is not None else self._now()
        merge_map = self._merge_map(workspace_id, as_of=as_of_moment)
        subject_ids = (
            self._closure_ids(self._resolve_id(subject, merge_map), merge_map)
            if subject is not None
            else None
        )
        object_ids = (
            self._closure_ids(self._resolve_id(object_id, merge_map), merge_map)
            if object_id is not None
            else None
        )
        return self._repo.query_facts(
            workspace_id,
            at=at_moment,
            as_of=as_of_moment,
            subject_ids=subject_ids,
            object_ids=object_ids,
            predicate_key=predicate,
            layer=layer,
            limit=limit,
            offset=offset,
        )

    def evidence_for(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[EvidenceData]]:
        return self._repo.evidence_for(workspace_id, fact_ids)

    # --- outcome feedback (spec J §2.5) --------------------------------- #

    def record_feedback(
        self,
        workspace_id: uuid.UUID,
        *,
        run_id: uuid.UUID,
        fact_ids: Sequence[uuid.UUID],
        outcome: FeedbackOutcome,
    ) -> int:
        """Write one outcome row per referenced fact; returns rows written.

        Facts that do not exist in this workspace are skipped (a stale or
        foreign ref can never create a row), and a fact the run already has
        a row for is skipped — the write is idempotent per ``(run_id,
        fact_id)`` so a replayed PR webhook changes nothing.
        """
        unique_ids = list(dict.fromkeys(fact_ids))
        known = self._repo.facts_by_ids(workspace_id, unique_ids)
        now = self._now()
        written = 0
        for fact_id in unique_ids:
            if fact_id not in known:
                continue
            if self._repo.insert_feedback(
                workspace_id, fact_id=fact_id, run_id=run_id, outcome=outcome, now=now
            ):
                written += 1
        return written

    def feedback_counts(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """``fact_id -> (reinforced, weakened)`` for the facts that have rows."""
        return self._repo.feedback_counts(workspace_id, fact_ids)

    def list_feedback(
        self,
        workspace_id: uuid.UUID,
        *,
        fact_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> list[FeedbackData]:
        return self._repo.list_feedback(workspace_id, fact_id=fact_id, run_id=run_id)

    def effective_confidences(
        self,
        workspace_id: uuid.UUID,
        facts: Sequence[FactData],
        *,
        now: datetime | None = None,
    ) -> dict[uuid.UUID, float]:
        """Every fact's effective confidence at ``now`` (default: the clock).

        One grouped feedback read for the whole batch; the decay/feedback
        arithmetic is :func:`forge_world.ledger.effective_confidence`.
        """
        moment = ensure_utc(now) if now is not None else self._now()
        counts = self._repo.feedback_counts(workspace_id, [fact.id for fact in facts])
        result: dict[uuid.UUID, float] = {}
        for fact in facts:
            reinforced, weakened = counts.get(fact.id, (0, 0))
            result[fact.id] = effective_confidence(
                fact, reinforced=reinforced, weakened=weakened, now=moment
            )
        return result

    def search_entities(
        self,
        workspace_id: uuid.UUID,
        *,
        q: str | None = None,
        type_key: str | None = None,
        at: datetime | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EntityData]:
        return self._repo.search_entities(
            workspace_id,
            q=q,
            type_key=type_key,
            at=ensure_utc(at) if at is not None else self._now(),
            as_of=ensure_utc(as_of) if as_of is not None else self._now(),
            limit=limit,
            offset=offset,
        )

    def get_entity_profile(
        self,
        workspace_id: uuid.UUID,
        entity_id: uuid.UUID,
        *,
        at: datetime | None = None,
        as_of: datetime | None = None,
    ) -> EntityProfile:
        at_moment = ensure_utc(at) if at is not None else self._now()
        as_of_moment = ensure_utc(as_of) if as_of is not None else self._now()
        self._entity_or_404(workspace_id, entity_id)
        merge_map = self._merge_map(workspace_id, as_of=as_of_moment)
        resolved_id = self._resolve_id(entity_id, merge_map)
        entity = self._entity_or_404(workspace_id, resolved_id)
        closure = self._closure_ids(resolved_id, merge_map)
        facts = self._repo.query_facts(
            workspace_id, at=at_moment, as_of=as_of_moment, subject_ids=closure
        )
        object_of = self._repo.query_facts(
            workspace_id, at=at_moment, as_of=as_of_moment, object_ids=closure
        )
        names = tuple(
            dict.fromkeys(
                fact.value for fact in facts if fact.predicate_key == KNOWN_AS_KEY and fact.value
            )
        )
        return EntityProfile(
            entity=entity,
            resolved_id=resolved_id,
            names=names,
            facts=tuple(facts),
            object_of=tuple(object_of),
        )

    def entity_changes(
        self,
        workspace_id: uuid.UUID,
        entity_id: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ChangeEvent]:
        """Record-axis events (asserted / invalidated) for one entity."""
        self._entity_or_404(workspace_id, entity_id)
        now = self._now()
        merge_map = self._merge_map(workspace_id, as_of=now)
        closure = self._closure_ids(self._resolve_id(entity_id, merge_map), merge_map)
        events: list[ChangeEvent] = []
        for fact in self._repo.facts_touching(workspace_id, closure):
            events.append(ChangeEvent("asserted", fact.id, ensure_utc(fact.recorded_at)))
            if fact.invalidated_at is not None:
                events.append(
                    ChangeEvent(
                        "invalidated",
                        fact.id,
                        ensure_utc(fact.invalidated_at),
                        reason=(
                            fact.invalidated_reason.value
                            if fact.invalidated_reason is not None
                            else None
                        ),
                    )
                )
        if since is not None:
            start = ensure_utc(since)
            events = [event for event in events if event.occurred_at >= start]
        if until is not None:
            end = ensure_utc(until)
            events = [event for event in events if event.occurred_at < end]
        events.sort(key=lambda event: (event.occurred_at, str(event.fact_id), event.kind))
        return events

    # --- proposals ------------------------------------------------------ #

    def _proposal_type_conflict(
        self, workspace_id: uuid.UUID, inp: StatementInput, *, now: datetime
    ) -> bool:
        """Declared domain/range vs the resolvable entities' types."""
        if inp.predicate_key is None:
            return False
        relation = self.get_relation(workspace_id, inp.predicate_key)
        if relation is None:
            raise WorldValidationError(f"unknown relation key {inp.predicate_key!r}")
        classes = self._classes_map(workspace_id)

        def _type_of(
            entity_id: uuid.UUID | None, name: str | None, stated: str | None
        ) -> str | None:
            if entity_id is not None:
                entity = self._repo.get_entity(workspace_id, entity_id)
                return entity.type_key if entity is not None else None
            if name is not None:
                recalled = self._recall(workspace_id, name, type_key=None, now=now)
                if recalled is not None and recalled.type_key is not None:
                    return recalled.type_key
            return stated

        if relation.domain_class is not None:
            subject_type = _type_of(inp.subject_id, inp.subject_name, inp.subject_type)
            if subject_type is not None and not types_compatible(
                subject_type, relation.domain_class, classes
            ):
                return True
        if relation.range_class not in (None, "value"):
            object_type = _type_of(inp.object_id, inp.object_name, inp.object_type)
            if object_type is not None and not types_compatible(
                object_type, relation.range_class, classes
            ):
                return True
        return False

    def create_proposal(
        self,
        workspace_id: uuid.UUID,
        inp: StatementInput,
        *,
        proposer_kind: SourceKind,
        proposer: str,
        rationale: str = "",
        verdict: Verdict | None = None,
        confidence: float | None = None,
    ) -> ProposalData:
        """Record a machine/person proposal with its shadow gate verdict."""
        if proposer_kind not in (SourceKind.PERSON, SourceKind.AGENT, SourceKind.MCP):
            raise WorldValidationError("proposer_kind must be person, agent or mcp")
        self._check_type(workspace_id, inp.subject_type)
        self._check_type(workspace_id, inp.object_type)
        now = self._now()
        identity = inp.identity()
        precedents: list[Precedent] = []
        for past in self._repo.list_decided_proposals(workspace_id):
            if past.payload.get("identity") != identity or past.decided_by is None:
                continue
            precedents.append(
                Precedent(
                    human=True,
                    decision="accept" if past.status is ProposalStatus.ACCEPTED else "reject",
                )
            )
        verdict_dict = gate(
            verdict,
            confidence,
            type_conflict=self._proposal_type_conflict(workspace_id, inp, now=now),
            precedents=precedents,
        ).to_dict()
        payload: dict[str, Any] = {
            "statement": inp.to_payload(),
            "identity": identity,
            "verdict": verdict,
            "confidence": confidence,
        }
        return self._repo.insert_proposal(
            workspace_id,
            kind="fact",
            payload=payload,
            proposer_kind=proposer_kind,
            proposer=proposer,
            rationale=rationale,
            gate=verdict_dict,
            now=now,
        )

    def list_proposals(
        self,
        workspace_id: uuid.UUID,
        *,
        status: ProposalStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProposalData]:
        return self._repo.list_proposals(workspace_id, status=status, limit=limit, offset=offset)

    def get_proposal(self, workspace_id: uuid.UUID, proposal_id: uuid.UUID) -> ProposalData:
        proposal = self._repo.get_proposal(workspace_id, proposal_id)
        if proposal is None:
            raise NotFoundError(f"no proposal {proposal_id}")
        return proposal

    def decide_proposal(
        self,
        workspace_id: uuid.UUID,
        proposal_id: uuid.UUID,
        *,
        decider: str,
        accept: bool,
        reason: str | None = None,
    ) -> ProposalDecisionOutcome:
        """Accept (asserting the fact) or reject a proposal, once.

        The decider must not be the proposer; rejection needs a reason.
        Nothing here ever auto-applies the shadow gate's verdict.
        """
        proposal = self.get_proposal(workspace_id, proposal_id)
        if proposal.status is not ProposalStatus.PROPOSED:
            raise ConflictError(f"proposal {proposal_id} was already decided")
        if decider == proposal.proposer:
            raise ForbiddenError("the proposer cannot decide their own proposal")
        now = self._now()
        fact: FactData | None = None
        assert_result: AssertResult | None = None
        if accept:
            statement_payload = proposal.payload.get("statement")
            if not isinstance(statement_payload, Mapping):
                raise WorldValidationError("proposal payload carries no statement")
            assert_result = self.assert_input(
                workspace_id,
                StatementInput.from_payload(statement_payload),
                source_kind=proposal.proposer_kind,
                actor=proposal.proposer,
                proposal_id=proposal.id,
            )
            fact = assert_result.fact
        elif not reason:
            raise WorldValidationError("rejecting a proposal needs a reason")
        decided = self._repo.decide_proposal(
            workspace_id,
            proposal_id,
            accept=accept,
            decided_by=decider,
            reason=reason,
            fact_id=fact.id if fact is not None else None,
            now=now,
        )
        return ProposalDecisionOutcome(proposal=decided, fact=fact, result=assert_result)

    # --- extraction ----------------------------------------------------- #

    def ingest(
        self,
        workspace_id: uuid.UUID,
        *,
        source_ref: str,
        title: str,
        text: str,
        doc_time: datetime | None = None,
        actor: str,
    ) -> IngestResult:
        """Extract facts from a document through the workspace model.

        Refuses (never fakes) when no model client is configured. Each
        chunk fails independently: a failed model call becomes a
        ``chunk_unextracted`` drop and the remaining chunks continue.
        """
        if self._model_client is None:
            raise ModelNotConfiguredError(
                "no model client is configured for world-model extraction; "
                "configure a workspace model provider to ingest documents"
            )
        client = self._model_client
        now = self._now()
        doc_time_utc = ensure_utc(doc_time) if doc_time is not None else None
        chunks = chunk_markdown(source_ref, text)
        doc_head = text[:1500]
        relations = self._relations_map(workspace_id)

        known_names: list[str] = []
        seen_names: set[str] = set()
        fact_ids: list[uuid.UUID] = []
        facts_created = 0
        duplicates = 0
        entities_created = 0
        drops_by_reason: dict[str, int] = {}
        touched_predicates: set[str] = set()
        # Described things are reused only within their own document:
        # earliest wins (list_entities is creation-ordered), keyed by the
        # exact normalised label so suffix stripping cannot merge
        # "the company" with "the corp".
        described_lookup: dict[str, EntityData] = {}
        for entity in self._repo.list_entities(workspace_id):
            if entity.described and entity.described_in == source_ref:
                described_lookup.setdefault(normalise_name(entity.label), entity)

        def described_entity(label: str) -> EntityData:
            nonlocal entities_created
            key = normalise_name(label)
            existing = described_lookup.get(key)
            if existing is not None:
                return existing
            created_entity = self._repo.insert_entity(
                workspace_id,
                label=label,
                described=True,
                described_in=source_ref,
                now=now,
            )
            described_lookup[key] = created_entity
            entities_created += 1
            return created_entity

        def note_name(name: str) -> None:
            folded = name.casefold()
            if folded not in seen_names:
                seen_names.add(folded)
                known_names.append(name)

        def note_drop(index: int, reason: DropReason, item: dict[str, Any] | None) -> None:
            drops_by_reason[reason.value] = drops_by_reason.get(reason.value, 0) + 1
            self._repo.insert_drop(
                workspace_id,
                source_ref=source_ref,
                chunk_index=index,
                reason=reason.value,
                item=item,
                now=now,
            )

        for index, chunk in enumerate(chunks):
            request = build_request(
                title=title,
                doc_head=doc_head,
                known_names=known_names,
                chunk_text=chunk.content,
                model=self._model,
            )
            try:
                response = client.complete(request)
            except Exception as exc:  # one chunk fails, the rest continue
                note_drop(
                    index,
                    DropReason.CHUNK_UNEXTRACTED,
                    {"error": f"{type(exc).__name__}: {exc}"[:512]},
                )
                continue
            if (
                response.stop_reason in PROVIDER_FAILURE_STOP_REASONS
                and not response.content.strip()
            ):
                note_drop(
                    index,
                    DropReason.CHUNK_UNEXTRACTED,
                    {"stop_reason": response.stop_reason},
                )
                continue

            raw_items, parse_drops = parse_reply(response.content)
            for drop in parse_drops:
                note_drop(index, drop.reason, drop.item)
            accepted, verify_drops = verify(
                chunk.content, doc_head, raw_items, known_names=known_names
            )
            for drop in verify_drops:
                note_drop(index, drop.reason, drop.item)

            for verified in accepted:
                evidence = (
                    source_ref,
                    index,
                    verified.quote,
                    verified.start_offset,
                    verified.end_offset,
                    doc_time_utc,
                )
                if verified.subject_kind == "described":
                    subject = described_entity(verified.subject)
                else:
                    subject, made, _typed = self._recall_or_create(
                        workspace_id,
                        verified.subject,
                        type_key=None,
                        source_kind=SourceKind.DOCUMENT,
                        actor=actor,
                        proposal_id=None,
                        evidence=evidence,
                        now=now,
                    )
                    entities_created += int(made)
                    note_name(verified.subject)

                object_id: uuid.UUID | None = None
                if verified.object_name is not None and verified.object_kind == "described":
                    object_id = described_entity(verified.object_name).id
                elif verified.object_name is not None:
                    target, made, _typed = self._recall_or_create(
                        workspace_id,
                        verified.object_name,
                        type_key=None,
                        source_kind=SourceKind.DOCUMENT,
                        actor=actor,
                        proposal_id=None,
                        evidence=evidence,
                        now=now,
                    )
                    entities_created += int(made)
                    note_name(verified.object_name)
                    object_id = target.id

                predicate_key: str | None = None
                layer = Layer.OPEN
                phrase_key = normalise_phrase(verified.phrase)
                matches = [
                    relation
                    for relation in relations.values()
                    if relation.kind is not RelationKind.STATE
                    and (
                        phrase_key == relation.key or phrase_key == normalise_phrase(relation.label)
                    )
                ]
                if len(matches) == 1:
                    predicate_key = matches[0].key
                    layer = Layer.TYPED

                statement = Statement(
                    subject_id=subject.id,
                    phrase=verified.phrase,
                    predicate_key=predicate_key,
                    object_id=object_id,
                    value=verified.value,
                    layer=layer,
                    mood=verified.mood,
                    qualifiers=dict(verified.qualifiers),
                    valid_from=verified.valid_from,
                    valid_to=verified.valid_to,
                )
                relation = relations.get(predicate_key) if predicate_key else None
                statement = self._adjust_for_relation(statement, relation)
                fact, created = self._repo.insert_fact(
                    workspace_id,
                    statement,
                    source_kind=SourceKind.DOCUMENT,
                    actor=actor,
                    now=now,
                )
                self._repo.insert_evidence(
                    workspace_id,
                    fact_id=fact.id,
                    source_ref=source_ref,
                    chunk_index=index,
                    quote=verified.quote,
                    start_offset=verified.start_offset,
                    end_offset=verified.end_offset,
                    doc_time=doc_time_utc,
                    now=now,
                )
                fact_ids.append(fact.id)
                if created:
                    facts_created += 1
                    if predicate_key is not None:
                        touched_predicates.add(predicate_key)
                else:
                    duplicates += 1

        for key in sorted(touched_predicates):
            self._close_after(workspace_id, relations.get(key), now=now)

        return IngestResult(
            chunks=len(chunks),
            facts_created=facts_created,
            duplicates=duplicates,
            entities_created=entities_created,
            drops=sum(drops_by_reason.values()),
            drops_by_reason=drops_by_reason,
            fact_ids=tuple(fact_ids),
        )

    # --- reasoning and conflicts ---------------------------------------- #

    def derive(self, workspace_id: uuid.UUID, *, actor: str) -> DeriveResult:
        """Rebuild the derived layer from the asserted typed facts.

        New consequences are inserted with proofs; live derived facts no
        longer entailed are invalidated as ``underived``. A second run over
        an unchanged base changes nothing.
        """
        now = self._now()
        relations = self.list_relations(workspace_id)
        asserted = self._repo.group_facts(workspace_id, as_of=now, layer=Layer.TYPED)
        existing = self._repo.group_facts(workspace_id, as_of=now, layer=Layer.DERIVED)
        derived, violations, capped = _derive_pure(asserted, relations)

        wanted = {dedup_key(item.to_statement()): item for item in derived}
        existing_by_key = {fact.dedup_key: fact for fact in existing}
        real_ids: dict[uuid.UUID, uuid.UUID] = {}
        for key, item in wanted.items():
            live = existing_by_key.get(key)
            if live is not None:
                real_ids[item.id] = live.id

        invalidated = 0
        for fact in existing:
            if fact.dedup_key not in wanted:
                self._repo.invalidate_fact(
                    workspace_id,
                    fact.id,
                    reason=InvalidatedReason.UNDERIVED,
                    now=now,
                )
                invalidated += 1

        created = 0
        for item in derived:  # depth order: premises precede consequences
            key = dedup_key(item.to_statement())
            if key in existing_by_key:
                continue
            fact, _ = self._repo.insert_fact(
                workspace_id,
                item.to_statement(),
                source_kind=SourceKind.RULE,
                actor=actor,
                now=now,
            )
            real_ids[item.id] = fact.id
            premises = [real_ids.get(premise, premise) for premise in item.premise_ids]
            self._repo.insert_derivation(
                workspace_id,
                fact_id=fact.id,
                rule=item.rule,
                premise_ids=premises,
                now=now,
            )
            created += 1

        return DeriveResult(
            created=created,
            invalidated=invalidated,
            capped=tuple(capped),
            violations=tuple(violations),
        )

    def conflicts(
        self,
        workspace_id: uuid.UUID,
        *,
        at: datetime | None = None,
        entity_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[Conflict]:
        """Functional overlaps + unordered successions + reasoning violations.

        With ``entity_ids``, only facts touching those entities (merge
        closures included, as subject or object) are considered, so a
        caller assembling context for a few entities does not pay for a
        whole-workspace derivation. The default considers every fact.
        """
        now = self._now()
        at_moment = ensure_utc(at) if at is not None else now
        relations = self._relations_map(workspace_id)

        merge_map = self._merge_map(workspace_id, as_of=now)

        def _remap(fact: FactData) -> FactData:
            return replace(
                fact,
                subject_id=self._resolve_id(fact.subject_id, merge_map),
                object_id=(
                    self._resolve_id(fact.object_id, merge_map)
                    if fact.object_id is not None
                    else None
                ),
            )

        if entity_ids is not None:
            if not entity_ids:
                return []
            closure: set[uuid.UUID] = set()
            for entity_id in entity_ids:
                closure.update(self._closure_ids(self._resolve_id(entity_id, merge_map), merge_map))
            closure_ids = sorted(closure, key=str)

            def _touching(facts: list[FactData], more: list[FactData]) -> list[FactData]:
                by_id = {fact.id: fact for fact in facts}
                for fact in more:
                    by_id.setdefault(fact.id, fact)
                return sorted(by_id.values(), key=lambda f: (f.recorded_at, str(f.id)))

            holding = _touching(
                self._repo.query_facts(
                    workspace_id, at=at_moment, as_of=now, subject_ids=closure_ids
                ),
                self._repo.query_facts(
                    workspace_id, at=at_moment, as_of=now, object_ids=closure_ids
                ),
            )
            held_all = _touching(
                self._repo.group_facts(workspace_id, as_of=now, subject_ids=closure_ids),
                self._repo.group_facts(workspace_id, as_of=now, object_ids=closure_ids),
            )
        else:
            holding = self._repo.query_facts(workspace_id, at=at_moment, as_of=now)
            held_all = self._repo.group_facts(workspace_id, as_of=now)
        found = functional_conflicts([_remap(fact) for fact in holding], relations)

        held = [fact for fact in held_all if fact.layer is not Layer.DERIVED]
        held_remapped = [_remap(fact) for fact in held]
        functional_keys = {
            relation.key
            for relation in relations.values()
            if relation.kind is RelationKind.STATE and relation.functional
        }
        inverse_keys = {
            relation.key
            for relation in relations.values()
            if relation.kind is RelationKind.STATE and relation.inverse_functional
        }
        _, unordered_subject = compute_ends(
            [fact for fact in held_remapped if fact.predicate_key in functional_keys],
            side="subject",
        )
        _, unordered_object = compute_ends(
            [fact for fact in held_remapped if fact.predicate_key in inverse_keys],
            side="object",
        )
        found.extend(unordered_subject)
        found.extend(unordered_object)

        asserted = [fact for fact in held if fact.layer is Layer.TYPED]
        _, violations, _ = _derive_pure(asserted, list(relations.values()))
        asserted_ids = {fact.id for fact in asserted}
        for violation in violations:
            fact_ids = [violation.asserted_fact_id]
            fact_ids.extend(
                premise
                for premise in violation.premise_ids
                if premise in asserted_ids and premise != violation.asserted_fact_id
            )
            found.append(
                Conflict(
                    kind="reasoning_violation",
                    predicate_key=violation.predicate_key,
                    subject_id=violation.subject_id,
                    object_id=violation.object_id,
                    fact_ids=tuple(fact_ids),
                    detail=violation.detail,
                )
            )
        found.sort(key=lambda c: (c.kind, c.predicate_key or "", tuple(str(i) for i in c.fact_ids)))
        return found

    # --- decisions -------------------------------------------------------- #

    def record_decision(
        self,
        workspace_id: uuid.UUID,
        *,
        title: str,
        outcome: str = "",
        actor: str,
        fact_ids: Sequence[uuid.UUID],
        links: Mapping[str, Any] | None = None,
        as_of: datetime | None = None,
        at: datetime | None = None,
    ) -> DecisionData:
        now = self._now()
        found = self._repo.facts_by_ids(workspace_id, list(fact_ids))
        missing = [str(fact_id) for fact_id in fact_ids if fact_id not in found]
        if missing:
            raise NotFoundError(f"no fact(s) {', '.join(missing)}")
        return self._repo.insert_decision(
            workspace_id,
            title=title,
            outcome=outcome,
            actor=actor,
            as_of=ensure_utc(as_of) if as_of is not None else now,
            at=ensure_utc(at) if at is not None else now,
            fact_ids=list(fact_ids),
            links=dict(links or {}),
            now=now,
        )

    def get_decision(self, workspace_id: uuid.UUID, decision_id: uuid.UUID) -> DecisionData:
        decision = self._repo.get_decision(workspace_id, decision_id)
        if decision is None:
            raise NotFoundError(f"no decision {decision_id}")
        return decision

    def decision_drift(self, workspace_id: uuid.UUID, decision_id: uuid.UUID) -> DriftReport:
        decision = self.get_decision(workspace_id, decision_id)
        rows = self._repo.facts_by_ids(workspace_id, list(decision.fact_ids))
        ordered = [rows[fact_id] for fact_id in decision.fact_ids if fact_id in rows]
        successors = self._repo.successors(workspace_id, list(decision.fact_ids))
        live_successors: dict[uuid.UUID, uuid.UUID] = {}
        for old_id, new_id in successors.items():
            successor = self._repo.get_fact(workspace_id, new_id)
            if successor is None:
                live_successors[old_id] = new_id
            else:
                live_successors[old_id] = self._live_version(workspace_id, successor).id
        return drift(ordered, live_successors)


__all__ = [
    "AssertResult",
    "ChangeEvent",
    "Clock",
    "DeriveResult",
    "EntityProfile",
    "IngestResult",
    "ProposalDecisionOutcome",
    "StatementInput",
    "WorldModelService",
]
