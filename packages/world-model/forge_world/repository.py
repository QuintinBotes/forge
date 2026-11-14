"""The world model's only SQL: workspace-scoped reads and appends.

Holds the single SQL rendering of each time predicate (``_holds_at_sql`` /
``_held_at_sql``; the Python renderings live in :mod:`forge_world.time`).
Append-only discipline is structural here: ``world_fact`` can be inserted
and tombstoned (:meth:`invalidate_fact`) but never otherwise updated or
deleted, and evidence, drops, derivations and decisions expose insert-only
paths — mirroring the Postgres triggers that enforce the same on the
production lane. Every method scopes by ``workspace_id``; callers own the
transaction (methods flush, never commit).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from forge_db.models.world import (
    WorldClass,
    WorldDecision,
    WorldDerivation,
    WorldEntity,
    WorldEntityMerge,
    WorldEvidence,
    WorldExtractionDrop,
    WorldFact,
    WorldFactFeedback,
    WorldProposal,
    WorldRelation,
)
from forge_world.ledger import (
    ClassDef,
    ConflictError,
    DecisionData,
    EntityData,
    EvidenceData,
    FactData,
    FeedbackData,
    FeedbackOutcome,
    InvalidatedReason,
    Layer,
    MergeData,
    Mood,
    NotFoundError,
    ProposalData,
    ProposalStatus,
    RelationDef,
    RelationKind,
    SourceKind,
    Statement,
    dedup_key,
)
from forge_world.time import Precision, ensure_utc


@dataclass(frozen=True)
class DerivationRecord:
    """One stored proof row (rule + premises) for a derived fact."""

    fact_id: uuid.UUID
    rule: str
    premise_ids: tuple[uuid.UUID, ...]


def _holds_at_sql(at: datetime) -> ColumnElement[bool]:
    """SQL rendering of :func:`forge_world.time.holds_at` (the only one)."""
    moment = ensure_utc(at)
    return and_(
        or_(WorldFact.valid_from.is_(None), WorldFact.valid_from <= moment),
        or_(WorldFact.valid_to.is_(None), WorldFact.valid_to > moment),
    )


def _held_at_sql(as_of: datetime) -> ColumnElement[bool]:
    """SQL rendering of :func:`forge_world.time.held_at` (the only one)."""
    moment = ensure_utc(as_of)
    return and_(
        WorldFact.created_at <= moment,
        or_(WorldFact.invalidated_at.is_(None), WorldFact.invalidated_at > moment),
    )


def _fact_data(row: WorldFact) -> FactData:
    return FactData(
        id=row.id,
        subject_id=row.subject_id,
        phrase=row.phrase,
        predicate_key=row.predicate_key,
        object_id=row.object_id,
        value=row.value,
        layer=Layer(row.layer),
        mood=Mood(row.mood) if row.mood is not None else None,
        qualifiers=dict(row.qualifiers),
        confidence=row.confidence,
        valid_from=ensure_utc(row.valid_from) if row.valid_from else None,
        valid_from_precision=Precision(row.valid_from_precision),
        valid_to=ensure_utc(row.valid_to) if row.valid_to else None,
        valid_to_precision=Precision(row.valid_to_precision),
        end_derived=row.end_derived,
        end_after_period=row.end_after_period,
        source_kind=SourceKind(row.source_kind),
        actor=row.actor,
        proposal_id=row.proposal_id,
        supersedes_id=row.supersedes_id,
        dedup_key=row.dedup_key,
        recorded_at=ensure_utc(row.created_at),
        invalidated_at=ensure_utc(row.invalidated_at) if row.invalidated_at else None,
        invalidated_reason=(
            InvalidatedReason(row.invalidated_reason) if row.invalidated_reason else None
        ),
    )


def _entity_data(row: WorldEntity) -> EntityData:
    return EntityData(
        id=row.id,
        label=row.label,
        type_key=row.type_key,
        type_source=row.type_source,
        described=row.described,
        described_in=row.described_in,
        created_at=ensure_utc(row.created_at),
    )


def _class_def(row: WorldClass) -> ClassDef:
    return ClassDef(
        key=row.key,
        label=row.label,
        parent_key=row.parent_key,
        disjoint_with=tuple(str(item) for item in row.disjoint_with),
        iri=row.iri,
        id=row.id,
    )


def _relation_def(row: WorldRelation) -> RelationDef:
    return RelationDef(
        key=row.key,
        label=row.label,
        kind=RelationKind(row.kind),
        domain_class=row.domain_class,
        range_class=row.range_class,
        functional=row.functional,
        inverse_functional=row.inverse_functional,
        transitive=row.transitive,
        symmetric=row.symmetric,
        inverse_of=row.inverse_of,
        sub_property_of=row.sub_property_of,
        iri=row.iri,
        id=row.id,
    )


def _proposal_data(row: WorldProposal) -> ProposalData:
    return ProposalData(
        id=row.id,
        kind=row.kind,
        payload=dict(row.payload),
        status=ProposalStatus(row.status),
        proposer_kind=SourceKind(row.proposer_kind),
        proposer=row.proposer,
        rationale=row.rationale,
        gate=dict(row.gate),
        decided_by=row.decided_by,
        decided_at=ensure_utc(row.decided_at) if row.decided_at else None,
        decision_reason=row.decision_reason,
        fact_id=row.fact_id,
        created_at=ensure_utc(row.created_at),
    )


def _merge_data(row: WorldEntityMerge) -> MergeData:
    return MergeData(
        id=row.id,
        survivor_id=row.survivor_id,
        merged_id=row.merged_id,
        actor=row.actor,
        reason=row.reason,
        created_at=ensure_utc(row.created_at),
        reverted_at=ensure_utc(row.reverted_at) if row.reverted_at else None,
        reverted_by=row.reverted_by,
        revert_reason=row.revert_reason,
    )


class WorldRepository:
    """Workspace-scoped SQL over the world tables (flush-only)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # --- ontology ------------------------------------------------------ #

    def insert_class(
        self, workspace_id: uuid.UUID, definition: ClassDef, *, now: datetime
    ) -> ClassDef:
        row = WorldClass(
            workspace_id=workspace_id,
            key=definition.key,
            label=definition.label,
            parent_key=definition.parent_key,
            disjoint_with=list(definition.disjoint_with),
            iri=definition.iri,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _class_def(row)

    def get_class(self, workspace_id: uuid.UUID, key: str) -> ClassDef | None:
        row = self._session.scalars(
            select(WorldClass).where(WorldClass.workspace_id == workspace_id, WorldClass.key == key)
        ).first()
        return _class_def(row) if row is not None else None

    def list_classes(self, workspace_id: uuid.UUID) -> list[ClassDef]:
        rows = self._session.scalars(
            select(WorldClass)
            .where(WorldClass.workspace_id == workspace_id)
            .order_by(WorldClass.key)
        ).all()
        return [_class_def(row) for row in rows]

    def insert_relation(
        self, workspace_id: uuid.UUID, definition: RelationDef, *, now: datetime
    ) -> RelationDef:
        row = WorldRelation(
            workspace_id=workspace_id,
            key=definition.key,
            label=definition.label,
            kind=definition.kind.value,
            domain_class=definition.domain_class,
            range_class=definition.range_class,
            functional=definition.functional,
            inverse_functional=definition.inverse_functional,
            transitive=definition.transitive,
            symmetric=definition.symmetric,
            inverse_of=definition.inverse_of,
            sub_property_of=definition.sub_property_of,
            iri=definition.iri,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _relation_def(row)

    def get_relation(self, workspace_id: uuid.UUID, key: str) -> RelationDef | None:
        row = self._session.scalars(
            select(WorldRelation).where(
                WorldRelation.workspace_id == workspace_id, WorldRelation.key == key
            )
        ).first()
        return _relation_def(row) if row is not None else None

    def list_relations(self, workspace_id: uuid.UUID) -> list[RelationDef]:
        rows = self._session.scalars(
            select(WorldRelation)
            .where(WorldRelation.workspace_id == workspace_id)
            .order_by(WorldRelation.key)
        ).all()
        return [_relation_def(row) for row in rows]

    # --- entities ------------------------------------------------------ #

    def insert_entity(
        self,
        workspace_id: uuid.UUID,
        *,
        label: str,
        type_key: str | None = None,
        type_source: str | None = None,
        described: bool = False,
        described_in: str | None = None,
        now: datetime,
    ) -> EntityData:
        row = WorldEntity(
            workspace_id=workspace_id,
            label=label,
            type_key=type_key,
            type_source=type_source,
            described=described,
            described_in=described_in,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _entity_data(row)

    def _entity_row(self, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> WorldEntity | None:
        row = self._session.get(WorldEntity, entity_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return row

    def get_entity(self, workspace_id: uuid.UUID, entity_id: uuid.UUID) -> EntityData | None:
        row = self._entity_row(workspace_id, entity_id)
        return _entity_data(row) if row is not None else None

    def list_entities(self, workspace_id: uuid.UUID) -> list[EntityData]:
        rows = self._session.scalars(
            select(WorldEntity)
            .where(WorldEntity.workspace_id == workspace_id)
            .order_by(WorldEntity.created_at, WorldEntity.id)
        ).all()
        return [_entity_data(row) for row in rows]

    def search_entities(
        self,
        workspace_id: uuid.UUID,
        *,
        q: str | None,
        type_key: str | None,
        at: datetime,
        as_of: datetime,
        limit: int,
        offset: int,
    ) -> list[EntityData]:
        stmt = select(WorldEntity).where(WorldEntity.workspace_id == workspace_id)
        if type_key is not None:
            stmt = stmt.where(WorldEntity.type_key == type_key)
        if q:
            needle = q.casefold()
            named = (
                select(WorldFact.subject_id)
                .where(
                    WorldFact.workspace_id == workspace_id,
                    WorldFact.predicate_key == "known_as",
                    func.lower(func.coalesce(WorldFact.value, "")).contains(needle),
                    _holds_at_sql(at),
                    _held_at_sql(as_of),
                )
                .scalar_subquery()
            )
            stmt = stmt.where(
                or_(
                    func.lower(WorldEntity.label).contains(needle),
                    WorldEntity.id.in_(named),
                )
            )
        rows = self._session.scalars(
            stmt.order_by(WorldEntity.label, WorldEntity.id).limit(limit).offset(offset)
        ).all()
        return [_entity_data(row) for row in rows]

    def set_entity_type(
        self,
        workspace_id: uuid.UUID,
        entity_id: uuid.UUID,
        *,
        type_key: str,
        source: str,
        now: datetime,
    ) -> EntityData:
        """Set an entity's type; a person-set type is final for models.

        When a model-sourced assignment would overwrite a person-set type it
        is refused as a no-op (the existing type stands).
        """
        row = self._entity_row(workspace_id, entity_id)
        if row is None:
            raise NotFoundError(f"no entity {entity_id}")
        if row.type_source == "person" and source != "person" and row.type_key is not None:
            return _entity_data(row)
        row.type_key = type_key
        row.type_source = source
        row.updated_at = now
        self._session.flush()
        return _entity_data(row)

    # --- facts --------------------------------------------------------- #

    def insert_fact(
        self,
        workspace_id: uuid.UUID,
        statement: Statement,
        *,
        source_kind: SourceKind,
        actor: str,
        proposal_id: uuid.UUID | None = None,
        supersedes_id: uuid.UUID | None = None,
        now: datetime,
    ) -> tuple[FactData, bool]:
        """Append a fact, or return the existing live duplicate.

        Dedup is the partial unique index, not a read-then-write check: the
        insert runs in a SAVEPOINT and an integrity conflict resolves to the
        live row the index protected. Returns ``(fact, created)``.
        """
        key = dedup_key(statement)
        row = WorldFact(
            workspace_id=workspace_id,
            subject_id=statement.subject_id,
            phrase=statement.phrase,
            predicate_key=statement.predicate_key,
            object_id=statement.object_id,
            value=statement.value,
            layer=statement.layer.value,
            mood=statement.mood.value if statement.mood is not None else None,
            qualifiers=dict(statement.qualifiers),
            confidence=statement.confidence,
            valid_from=statement.valid_from.instant,
            valid_from_precision=statement.valid_from.precision.value,
            valid_to=statement.valid_to.instant,
            valid_to_precision=statement.valid_to.precision.value,
            end_derived=statement.end_derived,
            end_after_period=statement.valid_to.end_after_period,
            source_kind=source_kind.value,
            actor=actor,
            proposal_id=proposal_id,
            supersedes_id=supersedes_id,
            dedup_key=key,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            existing = self._session.scalars(
                select(WorldFact).where(
                    WorldFact.workspace_id == workspace_id,
                    WorldFact.dedup_key == key,
                    WorldFact.invalidated_at.is_(None),
                )
            ).first()
            if existing is None:
                raise
            return _fact_data(existing), False
        return _fact_data(row), True

    def _fact_row(self, workspace_id: uuid.UUID, fact_id: uuid.UUID) -> WorldFact | None:
        row = self._session.get(WorldFact, fact_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return row

    def get_fact(self, workspace_id: uuid.UUID, fact_id: uuid.UUID) -> FactData | None:
        row = self._fact_row(workspace_id, fact_id)
        return _fact_data(row) if row is not None else None

    def invalidate_fact(
        self,
        workspace_id: uuid.UUID,
        fact_id: uuid.UUID,
        *,
        reason: InvalidatedReason,
        now: datetime,
    ) -> FactData:
        """The one permitted ``world_fact`` update: the tombstone stamp."""
        row = self._fact_row(workspace_id, fact_id)
        if row is None:
            raise NotFoundError(f"no fact {fact_id}")
        # Conditional on still-live in the database, so a concurrent
        # invalidation is a 409 here, not a trigger error (500) on flush.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(WorldFact)
                .where(
                    WorldFact.id == fact_id,
                    WorldFact.workspace_id == workspace_id,
                    WorldFact.invalidated_at.is_(None),
                )
                .values(
                    invalidated_at=now,
                    invalidated_reason=reason.value,
                    updated_at=now,
                )
            ),
        )
        if (result.rowcount or 0) != 1:
            raise ConflictError(f"fact {fact_id} is already invalidated")
        self._session.flush()
        self._session.refresh(row)
        return _fact_data(row)

    def query_facts(
        self,
        workspace_id: uuid.UUID,
        *,
        at: datetime,
        as_of: datetime,
        subject_ids: Sequence[uuid.UUID] | None = None,
        object_ids: Sequence[uuid.UUID] | None = None,
        predicate_key: str | None = None,
        layer: Layer | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[FactData]:
        """Facts holding at ``at`` as believed at ``as_of`` (both clocks)."""
        stmt = select(WorldFact).where(
            WorldFact.workspace_id == workspace_id,
            _holds_at_sql(at),
            _held_at_sql(as_of),
        )
        if subject_ids is not None:
            stmt = stmt.where(WorldFact.subject_id.in_(list(subject_ids)))
        if object_ids is not None:
            stmt = stmt.where(WorldFact.object_id.in_(list(object_ids)))
        if predicate_key is not None:
            stmt = stmt.where(WorldFact.predicate_key == predicate_key)
        if layer is not None:
            stmt = stmt.where(WorldFact.layer == layer.value)
        stmt = stmt.order_by(WorldFact.created_at, WorldFact.id)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        return [_fact_data(row) for row in self._session.scalars(stmt).all()]

    def facts_by_ids(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, FactData]:
        if not fact_ids:
            return {}
        rows = self._session.scalars(
            select(WorldFact).where(
                WorldFact.workspace_id == workspace_id,
                WorldFact.id.in_(list(fact_ids)),
            )
        ).all()
        return {row.id: _fact_data(row) for row in rows}

    def group_facts(
        self,
        workspace_id: uuid.UUID,
        *,
        as_of: datetime,
        predicate_key: str | None = None,
        layer: Layer | None = None,
        subject_ids: Sequence[uuid.UUID] | None = None,
        object_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[FactData]:
        """Facts held at ``as_of`` across all world times.

        The working set for temporal closing and reasoning, where filtering
        by ``holds_at(now)`` would hide future-dated successors and premises.
        Uses the one ``held_at`` SQL rendering; invalidated rows a caller
        still sees are only those invalidated after ``as_of``.
        """
        stmt = select(WorldFact).where(
            WorldFact.workspace_id == workspace_id,
            _held_at_sql(as_of),
        )
        if subject_ids is not None:
            stmt = stmt.where(WorldFact.subject_id.in_(list(subject_ids)))
        if object_ids is not None:
            stmt = stmt.where(WorldFact.object_id.in_(list(object_ids)))
        if predicate_key is not None:
            stmt = stmt.where(WorldFact.predicate_key == predicate_key)
        if layer is not None:
            stmt = stmt.where(WorldFact.layer == layer.value)
        stmt = stmt.order_by(WorldFact.created_at, WorldFact.id)
        return [_fact_data(row) for row in self._session.scalars(stmt).all()]

    def facts_touching(
        self, workspace_id: uuid.UUID, entity_ids: Sequence[uuid.UUID]
    ) -> list[FactData]:
        """Every fact (any status) whose subject is one of ``entity_ids``.

        The record-axis history behind the changes read; time filtering is
        the caller's, over ``recorded_at``/``invalidated_at``.
        """
        if not entity_ids:
            return []
        rows = self._session.scalars(
            select(WorldFact)
            .where(
                WorldFact.workspace_id == workspace_id,
                WorldFact.subject_id.in_(list(entity_ids)),
            )
            .order_by(WorldFact.created_at, WorldFact.id)
        ).all()
        return [_fact_data(row) for row in rows]

    def successors(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Map superseded fact id -> the (earliest) fact superseding it."""
        if not fact_ids:
            return {}
        rows = self._session.execute(
            select(WorldFact.supersedes_id, WorldFact.id)
            .where(
                WorldFact.workspace_id == workspace_id,
                WorldFact.supersedes_id.in_(list(fact_ids)),
            )
            .order_by(WorldFact.created_at, WorldFact.id)
        ).all()
        result: dict[uuid.UUID, uuid.UUID] = {}
        for supersedes_id, fact_id in rows:
            if supersedes_id is not None and supersedes_id not in result:
                result[supersedes_id] = fact_id
        return result

    # --- evidence, drops, derivations (append-only) -------------------- #

    def insert_evidence(
        self,
        workspace_id: uuid.UUID,
        *,
        fact_id: uuid.UUID,
        source_ref: str,
        chunk_index: int,
        quote: str,
        start_offset: int,
        end_offset: int,
        doc_time: datetime | None,
        now: datetime,
    ) -> EvidenceData:
        row = WorldEvidence(
            workspace_id=workspace_id,
            fact_id=fact_id,
            source_ref=source_ref,
            chunk_index=chunk_index,
            quote=quote,
            start_offset=start_offset,
            end_offset=end_offset,
            doc_time=doc_time,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return EvidenceData(
            id=row.id,
            fact_id=row.fact_id,
            source_ref=row.source_ref,
            chunk_index=row.chunk_index,
            quote=row.quote,
            start_offset=row.start_offset,
            end_offset=row.end_offset,
            doc_time=ensure_utc(row.doc_time) if row.doc_time else None,
        )

    def evidence_for(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[EvidenceData]]:
        if not fact_ids:
            return {}
        rows = self._session.scalars(
            select(WorldEvidence)
            .where(
                WorldEvidence.workspace_id == workspace_id,
                WorldEvidence.fact_id.in_(list(fact_ids)),
            )
            .order_by(WorldEvidence.created_at, WorldEvidence.id)
        ).all()
        result: dict[uuid.UUID, list[EvidenceData]] = {}
        for row in rows:
            result.setdefault(row.fact_id, []).append(
                EvidenceData(
                    id=row.id,
                    fact_id=row.fact_id,
                    source_ref=row.source_ref,
                    chunk_index=row.chunk_index,
                    quote=row.quote,
                    start_offset=row.start_offset,
                    end_offset=row.end_offset,
                    doc_time=ensure_utc(row.doc_time) if row.doc_time else None,
                )
            )
        return result

    def insert_drop(
        self,
        workspace_id: uuid.UUID,
        *,
        source_ref: str,
        chunk_index: int,
        reason: str,
        item: Mapping[str, Any] | None,
        now: datetime,
    ) -> None:
        self._session.add(
            WorldExtractionDrop(
                workspace_id=workspace_id,
                source_ref=source_ref,
                chunk_index=chunk_index,
                reason=reason,
                item=dict(item) if item is not None else None,
                created_at=now,
                updated_at=now,
            )
        )
        self._session.flush()

    def list_drops(
        self, workspace_id: uuid.UUID, *, source_ref: str | None = None
    ) -> list[tuple[str, int, str]]:
        """Stored drops as ``(source_ref, chunk_index, reason)`` (audit/tests)."""
        stmt = select(
            WorldExtractionDrop.source_ref,
            WorldExtractionDrop.chunk_index,
            WorldExtractionDrop.reason,
        ).where(WorldExtractionDrop.workspace_id == workspace_id)
        if source_ref is not None:
            stmt = stmt.where(WorldExtractionDrop.source_ref == source_ref)
        stmt = stmt.order_by(WorldExtractionDrop.created_at, WorldExtractionDrop.id)
        return [(ref, index, reason) for ref, index, reason in self._session.execute(stmt).all()]

    def insert_derivation(
        self,
        workspace_id: uuid.UUID,
        *,
        fact_id: uuid.UUID,
        rule: str,
        premise_ids: Sequence[uuid.UUID],
        now: datetime,
    ) -> None:
        self._session.add(
            WorldDerivation(
                workspace_id=workspace_id,
                fact_id=fact_id,
                rule=rule,
                premise_ids=[str(premise) for premise in premise_ids],
                created_at=now,
                updated_at=now,
            )
        )
        self._session.flush()

    def derivations_for(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[DerivationRecord]]:
        if not fact_ids:
            return {}
        rows = self._session.scalars(
            select(WorldDerivation).where(
                WorldDerivation.workspace_id == workspace_id,
                WorldDerivation.fact_id.in_(list(fact_ids)),
            )
        ).all()
        result: dict[uuid.UUID, list[DerivationRecord]] = {}
        for row in rows:
            result.setdefault(row.fact_id, []).append(
                DerivationRecord(
                    fact_id=row.fact_id,
                    rule=row.rule,
                    premise_ids=tuple(uuid.UUID(str(p)) for p in row.premise_ids),
                )
            )
        return result

    # --- proposals ----------------------------------------------------- #

    def insert_proposal(
        self,
        workspace_id: uuid.UUID,
        *,
        kind: str,
        payload: Mapping[str, Any],
        proposer_kind: SourceKind,
        proposer: str,
        rationale: str,
        gate: Mapping[str, Any],
        now: datetime,
    ) -> ProposalData:
        row = WorldProposal(
            workspace_id=workspace_id,
            kind=kind,
            payload=dict(payload),
            status=ProposalStatus.PROPOSED.value,
            proposer_kind=proposer_kind.value,
            proposer=proposer,
            rationale=rationale,
            gate=dict(gate),
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _proposal_data(row)

    def get_proposal(self, workspace_id: uuid.UUID, proposal_id: uuid.UUID) -> ProposalData | None:
        row = self._session.get(WorldProposal, proposal_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return _proposal_data(row)

    def list_proposals(
        self,
        workspace_id: uuid.UUID,
        *,
        status: ProposalStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProposalData]:
        stmt = select(WorldProposal).where(WorldProposal.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(WorldProposal.status == status.value)
        rows = self._session.scalars(
            stmt.order_by(WorldProposal.created_at.desc(), WorldProposal.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [_proposal_data(row) for row in rows]

    def list_decided_proposals(self, workspace_id: uuid.UUID) -> list[ProposalData]:
        rows = self._session.scalars(
            select(WorldProposal).where(
                WorldProposal.workspace_id == workspace_id,
                WorldProposal.status.in_(
                    [ProposalStatus.ACCEPTED.value, ProposalStatus.REJECTED.value]
                ),
            )
        ).all()
        return [_proposal_data(row) for row in rows]

    def decide_proposal(
        self,
        workspace_id: uuid.UUID,
        proposal_id: uuid.UUID,
        *,
        accept: bool,
        decided_by: str,
        reason: str | None,
        fact_id: uuid.UUID | None,
        now: datetime,
    ) -> ProposalData:
        """The proposal's single status transition (proposed -> decided)."""
        row = self._session.get(WorldProposal, proposal_id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFoundError(f"no proposal {proposal_id}")
        # The status condition is in the UPDATE itself, so two concurrent
        # decisions cannot both win: the loser's rowcount is 0 and its
        # transaction (including any fact insert) rolls back as a 409.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(WorldProposal)
                .where(
                    WorldProposal.id == proposal_id,
                    WorldProposal.workspace_id == workspace_id,
                    WorldProposal.status == ProposalStatus.PROPOSED.value,
                )
                .values(
                    status=(
                        ProposalStatus.ACCEPTED.value if accept else ProposalStatus.REJECTED.value
                    ),
                    decided_by=decided_by,
                    decided_at=now,
                    decision_reason=reason,
                    fact_id=fact_id,
                    updated_at=now,
                )
            ),
        )
        if (result.rowcount or 0) != 1:
            raise ConflictError(f"proposal {proposal_id} was already decided")
        self._session.flush()
        self._session.refresh(row)
        return _proposal_data(row)

    # --- merges -------------------------------------------------------- #

    def insert_merge(
        self,
        workspace_id: uuid.UUID,
        *,
        survivor_id: uuid.UUID,
        merged_id: uuid.UUID,
        actor: str,
        reason: str,
        now: datetime,
    ) -> MergeData:
        row = WorldEntityMerge(
            workspace_id=workspace_id,
            survivor_id=survivor_id,
            merged_id=merged_id,
            actor=actor,
            reason=reason,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"entity {merged_id} already has a live merge") from exc
        return _merge_data(row)

    def get_merge(self, workspace_id: uuid.UUID, merge_id: uuid.UUID) -> MergeData | None:
        row = self._session.get(WorldEntityMerge, merge_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return _merge_data(row)

    def list_merges(self, workspace_id: uuid.UUID, *, as_of: datetime) -> list[MergeData]:
        """Merges live at ``as_of`` on the record clock (revert-aware)."""
        moment = ensure_utc(as_of)
        rows = self._session.scalars(
            select(WorldEntityMerge).where(
                WorldEntityMerge.workspace_id == workspace_id,
                WorldEntityMerge.created_at <= moment,
                or_(
                    WorldEntityMerge.reverted_at.is_(None),
                    WorldEntityMerge.reverted_at > moment,
                ),
            )
        ).all()
        return [_merge_data(row) for row in rows]

    def revert_merge(
        self,
        workspace_id: uuid.UUID,
        merge_id: uuid.UUID,
        *,
        reverted_by: str,
        reason: str | None,
        now: datetime,
    ) -> MergeData:
        row = self._session.get(WorldEntityMerge, merge_id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFoundError(f"no merge {merge_id}")
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(WorldEntityMerge)
                .where(
                    WorldEntityMerge.id == merge_id,
                    WorldEntityMerge.workspace_id == workspace_id,
                    WorldEntityMerge.reverted_at.is_(None),
                )
                .values(
                    reverted_at=now,
                    reverted_by=reverted_by,
                    revert_reason=reason,
                    updated_at=now,
                )
            ),
        )
        if (result.rowcount or 0) != 1:
            raise ConflictError(f"merge {merge_id} was already reverted")
        self._session.flush()
        self._session.refresh(row)
        return _merge_data(row)

    # --- decisions (append-only) --------------------------------------- #

    def insert_decision(
        self,
        workspace_id: uuid.UUID,
        *,
        title: str,
        outcome: str,
        actor: str,
        as_of: datetime,
        at: datetime,
        fact_ids: Sequence[uuid.UUID],
        links: Mapping[str, Any],
        now: datetime,
    ) -> DecisionData:
        row = WorldDecision(
            workspace_id=workspace_id,
            title=title,
            outcome=outcome,
            actor=actor,
            as_of=as_of,
            at=at,
            fact_ids=[str(fact_id) for fact_id in fact_ids],
            links=dict(links),
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return DecisionData(
            id=row.id,
            title=row.title,
            outcome=row.outcome,
            actor=row.actor,
            as_of=ensure_utc(row.as_of),
            at=ensure_utc(row.at),
            fact_ids=tuple(uuid.UUID(str(f)) for f in row.fact_ids),
            links=dict(row.links),
            created_at=ensure_utc(row.created_at),
        )

    def get_decision(self, workspace_id: uuid.UUID, decision_id: uuid.UUID) -> DecisionData | None:
        row = self._session.get(WorldDecision, decision_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return DecisionData(
            id=row.id,
            title=row.title,
            outcome=row.outcome,
            actor=row.actor,
            as_of=ensure_utc(row.as_of),
            at=ensure_utc(row.at),
            fact_ids=tuple(uuid.UUID(str(f)) for f in row.fact_ids),
            links=dict(row.links),
            created_at=ensure_utc(row.created_at),
        )

    # --- outcome feedback (append-only, spec J §2.5) -------------------- #

    def insert_feedback(
        self,
        workspace_id: uuid.UUID,
        *,
        fact_id: uuid.UUID,
        run_id: uuid.UUID,
        outcome: FeedbackOutcome,
        now: datetime,
    ) -> bool:
        """Append one feedback row; ``False`` when ``(run_id, fact_id)`` exists.

        Idempotency is the unique constraint, not a read-then-write race:
        the insert runs in a SAVEPOINT and a conflict resolves to ``False``
        (a replayed webhook writes nothing new).
        """
        row = WorldFactFeedback(
            workspace_id=workspace_id,
            fact_id=fact_id,
            run_id=run_id,
            outcome=outcome.value,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(row)
                self._session.flush()
        except IntegrityError:
            return False
        return True

    def feedback_counts(
        self, workspace_id: uuid.UUID, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """``fact_id -> (reinforced, weakened)`` for the facts that have rows."""
        if not fact_ids:
            return {}
        rows = self._session.execute(
            select(
                WorldFactFeedback.fact_id,
                WorldFactFeedback.outcome,
                func.count(),
            )
            .where(
                WorldFactFeedback.workspace_id == workspace_id,
                WorldFactFeedback.fact_id.in_(list(fact_ids)),
            )
            .group_by(WorldFactFeedback.fact_id, WorldFactFeedback.outcome)
        ).all()
        counts: dict[uuid.UUID, tuple[int, int]] = {}
        for fact_id, outcome, total in rows:
            reinforced, weakened = counts.get(fact_id, (0, 0))
            if outcome == FeedbackOutcome.REINFORCED.value:
                reinforced += int(total)
            else:
                weakened += int(total)
            counts[fact_id] = (reinforced, weakened)
        return counts

    def list_feedback(
        self,
        workspace_id: uuid.UUID,
        *,
        fact_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> list[FeedbackData]:
        stmt = select(WorldFactFeedback).where(WorldFactFeedback.workspace_id == workspace_id)
        if fact_id is not None:
            stmt = stmt.where(WorldFactFeedback.fact_id == fact_id)
        if run_id is not None:
            stmt = stmt.where(WorldFactFeedback.run_id == run_id)
        stmt = stmt.order_by(WorldFactFeedback.created_at, WorldFactFeedback.id)
        return [
            FeedbackData(
                id=row.id,
                fact_id=row.fact_id,
                run_id=row.run_id,
                outcome=FeedbackOutcome(row.outcome),
                created_at=ensure_utc(row.created_at),
            )
            for row in self._session.scalars(stmt).all()
        ]


__all__ = ["DerivationRecord", "WorldRepository"]
