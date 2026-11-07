"""Request/response schemas for the ``/world`` router (world model)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_world import Layer, Mood, RelationKind, StatementInput


class StatementIn(BaseModel):
    """A claim as callers state it: entities by id or by name.

    ``valid_from``/``valid_to`` take ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``
    or a full ISO-8601 instant (precision comes from the form); omitted
    means unknown/open.
    """

    model_config = ConfigDict(populate_by_name=True)

    subject_id: uuid.UUID | None = None
    subject: str | None = None
    subject_type: str | None = None
    phrase: str = Field(min_length=1)
    predicate_key: str | None = None
    object_id: uuid.UUID | None = None
    object_name: str | None = Field(default=None, alias="object")
    object_type: str | None = None
    value: str | None = None
    layer: Layer | None = None
    mood: Mood | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    valid_from: str | None = None
    valid_to: str | None = None

    def to_input(self) -> StatementInput:
        return StatementInput(
            phrase=self.phrase,
            subject_id=self.subject_id,
            subject_name=self.subject,
            subject_type=self.subject_type,
            predicate_key=self.predicate_key,
            object_id=self.object_id,
            object_name=self.object_name,
            object_type=self.object_type,
            value=self.value,
            layer=self.layer,
            mood=self.mood,
            qualifiers=dict(self.qualifiers),
            confidence=self.confidence,
            valid_from_text=self.valid_from,
            valid_to_text=self.valid_to,
        )


class EvidenceOut(BaseModel):
    id: uuid.UUID
    source_ref: str
    chunk_index: int
    quote: str
    start_offset: int
    end_offset: int
    doc_time: datetime | None = None


class FactOut(BaseModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    phrase: str
    predicate_key: str | None
    object_id: uuid.UUID | None
    value: str | None
    layer: str
    mood: str | None
    qualifiers: dict[str, Any]
    confidence: float
    valid_from: datetime | None
    valid_from_precision: str
    valid_to: datetime | None
    valid_to_precision: str
    end_derived: bool
    end_after_period: bool
    source_kind: str
    actor: str
    proposal_id: uuid.UUID | None
    supersedes_id: uuid.UUID | None
    recorded_at: datetime
    invalidated_at: datetime | None
    invalidated_reason: str | None
    evidence: list[EvidenceOut] = Field(default_factory=list)


class FactWriteOut(FactOut):
    created: bool


class FactListOut(BaseModel):
    items: list[FactOut]


class EntityOut(BaseModel):
    id: uuid.UUID
    label: str
    type_key: str | None
    type_source: str | None
    described: bool
    described_in: str | None
    created_at: datetime


class EntityListOut(BaseModel):
    items: list[EntityOut]
    limit: int
    offset: int


class EntityProfileOut(BaseModel):
    entity: EntityOut
    resolved_id: uuid.UUID
    names: list[str]
    facts: list[FactOut]
    object_of: list[FactOut]


class ChangeOut(BaseModel):
    kind: str
    fact_id: uuid.UUID
    occurred_at: datetime
    reason: str | None = None


class ChangesOut(BaseModel):
    items: list[ChangeOut]


class IngestIn(BaseModel):
    source_ref: str = Field(min_length=1)
    title: str = ""
    text: str
    doc_time: datetime | None = None


class IngestOut(BaseModel):
    source_ref: str
    chunks: int
    facts_created: int
    duplicates: int
    entities_created: int
    drops: int
    drops_by_reason: dict[str, int]
    fact_ids: list[uuid.UUID]


class ProposalIn(StatementIn):
    """A proposed fact plus its rationale and (optional) gate inputs."""

    rationale: str = ""
    # No proposer_kind: provenance comes from the authenticated principal, never the caller.
    verdict: Literal["accept", "reject", "unsure"] | None = None
    gate_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ProposalOut(BaseModel):
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    status: str
    proposer_kind: str
    proposer: str
    rationale: str
    gate: dict[str, Any]
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    fact_id: uuid.UUID | None
    created_at: datetime


class ProposalListOut(BaseModel):
    items: list[ProposalOut]


class ProposalDecisionIn(BaseModel):
    accept: bool
    reason: str | None = None


class ProposalDecisionOut(BaseModel):
    proposal: ProposalOut
    fact: FactOut | None = None


class MergeIn(BaseModel):
    survivor_id: uuid.UUID
    merged_id: uuid.UUID
    reason: str = ""


class RevertIn(BaseModel):
    reason: str | None = None


class MergeOut(BaseModel):
    id: uuid.UUID
    survivor_id: uuid.UUID
    merged_id: uuid.UUID
    actor: str
    reason: str
    created_at: datetime
    reverted_at: datetime | None
    reverted_by: str | None
    revert_reason: str | None


class ViolationOut(BaseModel):
    predicate_key: str
    subject_id: uuid.UUID
    object_id: uuid.UUID | None
    value: str | None
    asserted_fact_id: uuid.UUID
    rule: str
    premise_ids: list[uuid.UUID]
    detail: str


class DeriveOut(BaseModel):
    created: int
    invalidated: int
    capped: list[str]
    violations: list[ViolationOut]


class ConflictOut(BaseModel):
    kind: str
    predicate_key: str | None
    subject_id: uuid.UUID | None
    object_id: uuid.UUID | None
    fact_ids: list[uuid.UUID]
    detail: str


class ConflictsOut(BaseModel):
    items: list[ConflictOut]


class ClassIn(BaseModel):
    key: str
    label: str
    parent_key: str | None = None
    disjoint_with: list[str] = Field(default_factory=list)
    iri: str | None = None


class ClassOut(BaseModel):
    id: uuid.UUID | None = None
    key: str
    label: str
    parent_key: str | None
    disjoint_with: list[str]
    iri: str | None


class ClassesOut(BaseModel):
    items: list[ClassOut]


class RelationIn(BaseModel):
    key: str
    label: str
    kind: RelationKind
    domain_class: str | None = None
    range_class: str | None = None
    functional: bool = False
    inverse_functional: bool = False
    transitive: bool = False
    symmetric: bool = False
    inverse_of: str | None = None
    sub_property_of: str | None = None
    iri: str | None = None


class RelationOut(BaseModel):
    id: uuid.UUID | None = None
    key: str
    label: str
    kind: str
    domain_class: str | None
    range_class: str | None
    functional: bool
    inverse_functional: bool
    transitive: bool
    symmetric: bool
    inverse_of: str | None
    sub_property_of: str | None
    iri: str | None


class RelationsOut(BaseModel):
    items: list[RelationOut]


class DecisionIn(BaseModel):
    title: str = Field(min_length=1)
    outcome: str = ""
    fact_ids: list[uuid.UUID]
    links: dict[str, Any] = Field(default_factory=dict)
    as_of: datetime | None = None
    at: datetime | None = None


class DecisionOut(BaseModel):
    id: uuid.UUID
    title: str
    outcome: str
    actor: str
    as_of: datetime
    at: datetime
    fact_ids: list[uuid.UUID]
    links: dict[str, Any]
    created_at: datetime


class DriftItemOut(BaseModel):
    fact_id: uuid.UUID
    status: str
    reason: str | None = None
    superseded_by: uuid.UUID | None = None


class DriftOut(BaseModel):
    decision_id: uuid.UUID
    drifted: bool
    items: list[DriftItemOut]


__all__ = [
    "ChangeOut",
    "ChangesOut",
    "ClassIn",
    "ClassOut",
    "ClassesOut",
    "ConflictOut",
    "ConflictsOut",
    "DecisionIn",
    "DecisionOut",
    "DeriveOut",
    "DriftItemOut",
    "DriftOut",
    "EntityListOut",
    "EntityOut",
    "EntityProfileOut",
    "EvidenceOut",
    "FactListOut",
    "FactOut",
    "FactWriteOut",
    "IngestIn",
    "IngestOut",
    "MergeIn",
    "MergeOut",
    "ProposalDecisionIn",
    "ProposalDecisionOut",
    "ProposalIn",
    "ProposalListOut",
    "ProposalOut",
    "RelationIn",
    "RelationOut",
    "RelationsOut",
    "RevertIn",
    "StatementIn",
    "ViolationOut",
]
