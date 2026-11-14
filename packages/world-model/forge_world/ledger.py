"""Ledger vocabulary and pure fact shapes: enums, statements, dedup.

Pure module — no I/O. Defines the frozen data views every other module
shares (``FactData`` mirrors a ``world_fact`` row; the repository is the only
place that builds one from SQL) and the dedup identity the database enforces
with its partial unique index.
"""

from __future__ import annotations

import enum
import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forge_world.time import Precision, WorldBound, ensure_utc


class Layer(enum.StrEnum):
    OPEN = "open"
    TYPED = "typed"
    DERIVED = "derived"
    MEMORY = "memory"


class Mood(enum.StrEnum):
    PLAN = "plan"
    REQUIREMENT = "requirement"
    CONDITION = "condition"


class SourceKind(enum.StrEnum):
    DOCUMENT = "document"
    PERSON = "person"
    AGENT = "agent"
    RULE = "rule"
    MCP = "mcp"


class RelationKind(enum.StrEnum):
    STATE = "state"
    EVENT = "event"
    ETERNAL = "eternal"


class InvalidatedReason(enum.StrEnum):
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    UNDERIVED = "underived"
    SOURCE_DELETED = "source_deleted"
    MERGED_AWAY = "merged_away"


class ProposalStatus(enum.StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class WorldModelError(Exception):
    """Base for every world-model domain failure."""


class NotFoundError(WorldModelError):
    """The addressed world-model resource does not exist in this workspace."""


class ConflictError(WorldModelError):
    """The request conflicts with current ledger state (HTTP 409)."""


class ForbiddenError(WorldModelError, PermissionError):
    """The actor may not perform this ledger operation (HTTP 403)."""


class WorldValidationError(WorldModelError, ValueError):
    """The request is malformed against the ledger's rules (HTTP 422)."""


class ModelNotConfiguredError(WorldModelError):
    """No model client is configured, so extraction must refuse (HTTP 503)."""


#: Ontology keys are the API tokens.
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")

KNOWN_AS_KEY = "known_as"


def validate_key(key: str, *, what: str = "key") -> str:
    """Return ``key`` if it is a legal ontology key, else raise."""
    if not KEY_RE.fullmatch(key):
        raise WorldValidationError(f"{what} {key!r} must match ^[a-z0-9_]{{1,40}}$")
    return key


def normalise_phrase(phrase: str) -> str:
    """Canonical phrase form for identity: casefolded, whitespace-collapsed."""
    return " ".join(phrase.casefold().split())


def normalise_value(value: str) -> str:
    """Canonical literal form for identity: casefolded, whitespace-collapsed."""
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class Statement:
    """A fully resolved claim ready to assert (entities already identified).

    Exactly one of ``object_id``/``value`` is set (the DB CHECK mirrors this).
    Bounds are stored-form :class:`WorldBound` (bucket start/end applied).
    """

    subject_id: uuid.UUID
    phrase: str
    predicate_key: str | None
    object_id: uuid.UUID | None
    value: str | None
    layer: Layer
    mood: Mood | None = None
    qualifiers: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    valid_from: WorldBound = field(default_factory=WorldBound.unknown)
    valid_to: WorldBound = field(default_factory=WorldBound.unknown)
    end_derived: bool = False

    def __post_init__(self) -> None:
        if not self.phrase.strip():
            raise WorldValidationError("a statement needs a non-empty phrase")
        if (self.object_id is None) == (self.value is None):
            raise WorldValidationError("exactly one of object_id and value must be set")
        if not 0.0 <= self.confidence <= 1.0:
            raise WorldValidationError("confidence must be within 0..1")
        if (
            self.valid_from.instant is not None
            and self.valid_to.instant is not None
            and ensure_utc(self.valid_from.instant) >= ensure_utc(self.valid_to.instant)
        ):
            # An empty [from, to) span holds at no instant; in particular a
            # state span whose two ends are equal is refused.
            raise WorldValidationError("valid_from must be before valid_to")


def _instant_part(bound: WorldBound) -> str:
    return "" if bound.instant is None else ensure_utc(bound.instant).isoformat()


def dedup_key(statement: Statement) -> str:
    """The live-fact identity the partial unique index enforces.

    ``sha256(subject_id | normalised predicate or phrase | object_id or
    normalised value | mood | layer | valid_from | valid_to)`` — bounds by
    stored instant, so a temporal-closed copy (different ``valid_to``) is a
    different fact from the open-ended one it supersedes.
    """
    predicate = statement.predicate_key or normalise_phrase(statement.phrase)
    target = (
        str(statement.object_id)
        if statement.object_id is not None
        else normalise_value(statement.value or "")
    )
    parts = (
        str(statement.subject_id),
        predicate,
        target,
        statement.mood.value if statement.mood is not None else "",
        statement.layer.value,
        _instant_part(statement.valid_from),
        _instant_part(statement.valid_to),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FactData:
    """A ``world_fact`` row as pure data (satisfies ``time.FactTimes``)."""

    id: uuid.UUID
    subject_id: uuid.UUID
    phrase: str
    predicate_key: str | None
    object_id: uuid.UUID | None
    value: str | None
    layer: Layer
    mood: Mood | None
    qualifiers: Mapping[str, Any]
    confidence: float
    valid_from: datetime | None
    valid_from_precision: Precision
    valid_to: datetime | None
    valid_to_precision: Precision
    end_derived: bool
    end_after_period: bool
    source_kind: SourceKind
    actor: str
    proposal_id: uuid.UUID | None
    supersedes_id: uuid.UUID | None
    dedup_key: str
    recorded_at: datetime
    invalidated_at: datetime | None
    invalidated_reason: InvalidatedReason | None

    @property
    def from_bound(self) -> WorldBound:
        if self.valid_from is None:
            return WorldBound.unknown()
        return WorldBound(instant=ensure_utc(self.valid_from), precision=self.valid_from_precision)

    @property
    def to_bound(self) -> WorldBound:
        if self.valid_to is None:
            return WorldBound.unknown()
        return WorldBound(
            instant=ensure_utc(self.valid_to),
            precision=self.valid_to_precision,
            end_after_period=self.end_after_period,
        )

    @property
    def is_live(self) -> bool:
        return self.invalidated_at is None

    def to_statement(self) -> Statement:
        """This fact as an assertable statement (for supersession copies)."""
        return Statement(
            subject_id=self.subject_id,
            phrase=self.phrase,
            predicate_key=self.predicate_key,
            object_id=self.object_id,
            value=self.value,
            layer=self.layer,
            mood=self.mood,
            qualifiers=dict(self.qualifiers),
            confidence=self.confidence,
            valid_from=self.from_bound,
            valid_to=self.to_bound,
            end_derived=self.end_derived,
        )


@dataclass(frozen=True)
class ClassDef:
    """A ``world_class`` row as pure data."""

    key: str
    label: str
    parent_key: str | None = None
    disjoint_with: tuple[str, ...] = ()
    iri: str | None = None
    id: uuid.UUID | None = None


@dataclass(frozen=True)
class RelationDef:
    """A ``world_relation`` row as pure data (also the system ``known_as``)."""

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
    id: uuid.UUID | None = None


#: The one system relation present in every workspace without declaration:
#: every name is a fact. A workspace row with this key shadows it.
KNOWN_AS_RELATION = RelationDef(
    key=KNOWN_AS_KEY, label="known as", kind=RelationKind.STATE, range_class="value"
)


@dataclass(frozen=True)
class EntityData:
    """A ``world_entity`` row as pure data."""

    id: uuid.UUID
    label: str
    type_key: str | None
    type_source: str | None
    described: bool
    described_in: str | None
    created_at: datetime


@dataclass(frozen=True)
class EvidenceData:
    """A ``world_evidence`` row as pure data."""

    id: uuid.UUID
    fact_id: uuid.UUID
    source_ref: str
    chunk_index: int
    quote: str
    start_offset: int
    end_offset: int
    doc_time: datetime | None


@dataclass(frozen=True)
class ProposalData:
    """A ``world_proposal`` row as pure data."""

    id: uuid.UUID
    kind: str
    payload: Mapping[str, Any]
    status: ProposalStatus
    proposer_kind: SourceKind
    proposer: str
    rationale: str
    gate: Mapping[str, Any]
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    fact_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True)
class MergeData:
    """A ``world_entity_merge`` row as pure data."""

    id: uuid.UUID
    survivor_id: uuid.UUID
    merged_id: uuid.UUID
    actor: str
    reason: str
    created_at: datetime
    reverted_at: datetime | None
    reverted_by: str | None
    revert_reason: str | None


@dataclass(frozen=True)
class DecisionData:
    """A ``world_decision`` row as pure data."""

    id: uuid.UUID
    title: str
    outcome: str
    actor: str
    as_of: datetime
    at: datetime
    fact_ids: tuple[uuid.UUID, ...]
    links: Mapping[str, Any]
    created_at: datetime


__all__ = [
    "KEY_RE",
    "KNOWN_AS_KEY",
    "KNOWN_AS_RELATION",
    "ClassDef",
    "ConflictError",
    "DecisionData",
    "EntityData",
    "EvidenceData",
    "FactData",
    "ForbiddenError",
    "InvalidatedReason",
    "Layer",
    "MergeData",
    "ModelNotConfiguredError",
    "Mood",
    "NotFoundError",
    "ProposalData",
    "ProposalStatus",
    "RelationDef",
    "RelationKind",
    "SourceKind",
    "Statement",
    "WorldModelError",
    "WorldValidationError",
    "dedup_key",
    "normalise_phrase",
    "normalise_value",
    "validate_key",
]
