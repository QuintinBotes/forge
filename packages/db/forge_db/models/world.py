"""World-model tables — the append-only bitemporal fact ledger.

One row per claim about the world, carrying two clocks: the *world* interval
(``valid_from``/``valid_to`` + per-bound precision, when the claim was true)
and the *record* interval (``created_at``/``invalidated_at``, when Forge
believed it). ``world_fact`` rows are never updated except for the one-way
tombstone (``invalidated_at``/``invalidated_reason`` move from NULL to a value
exactly once) and never deleted; a Postgres trigger enforces both, and a
partial unique index on ``(workspace_id, dedup_key) WHERE invalidated_at IS
NULL`` makes a duplicate live fact impossible even under parallel ingest.
Evidence, extraction drops, derivations and decisions are fully append-only
via the shared immutability trigger. On SQLite (the unit lane) the triggers
are no-ops and the repository layer is the guard: it exposes no update/delete
path for the append-only tables beyond the tombstone.

Enum-shaped columns are plain strings validated by ``forge_world`` (the domain
package depends on ``forge_db``, never the reverse). Foreign keys pointing into
append-only tables use ``CASCADE`` like ``Attestation`` (see its docstring for
the accepted tension: parent rows are never hard-deleted in practice).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.expression import FromClause

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type

#: Name of the plpgsql function guarding ``world_fact``'s one-way tombstone.
_TOMBSTONE_FN = "forge_world_fact_guard"


def _attach_tombstone_trigger(table: FromClause) -> None:
    """Guard ``world_fact`` on Postgres: DELETE raises; UPDATE may only stamp.

    Mirrors :func:`forge_db.base.attach_immutability_trigger`'s registration
    (``after_create``/``before_drop`` DDL, Postgres-only) but allows exactly one
    transition — ``invalidated_at``/``invalidated_reason`` moving from NULL to
    a value on a still-live row. Any other column change, a second stamp, or a
    DELETE raises. The ``NEW.invalidated_at IS NULL`` check also refuses
    updated_at-only touch updates: every permitted UPDATE is a tombstone.
    """
    if not isinstance(table, Table):  # pragma: no cover - defensive, never hit
        raise TypeError("_attach_tombstone_trigger requires a mapped Table")

    create_fn = DDL(
        f"""
        CREATE OR REPLACE FUNCTION {_TOMBSTONE_FN}() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'world_fact is append-only';
            END IF;
            IF OLD.invalidated_at IS NOT NULL THEN
                RAISE EXCEPTION 'world_fact %% is already invalidated', OLD.id;
            END IF;
            IF (to_jsonb(NEW) - 'invalidated_at' - 'invalidated_reason' - 'updated_at')
               IS DISTINCT FROM
               (to_jsonb(OLD) - 'invalidated_at' - 'invalidated_reason' - 'updated_at')
            THEN
                RAISE EXCEPTION 'world_fact %% may only be invalidated', OLD.id;
            END IF;
            IF NEW.invalidated_at IS NULL THEN
                RAISE EXCEPTION 'world_fact %% may only be invalidated', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    trigger_name = f"{table.name}_tombstone"
    create_trigger = DDL(
        f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE OR DELETE ON {table.name}
        FOR EACH ROW EXECUTE FUNCTION {_TOMBSTONE_FN}();
        """
    )
    drop_trigger = DDL(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table.name};")

    event.listen(table, "after_create", create_fn.execute_if(dialect="postgresql"))
    event.listen(table, "after_create", create_trigger.execute_if(dialect="postgresql"))
    event.listen(table, "before_drop", drop_trigger.execute_if(dialect="postgresql"))
    event.listen(
        table,
        "after_drop",
        DDL(f"DROP FUNCTION IF EXISTS {_TOMBSTONE_FN}();").execute_if(dialect="postgresql"),
    )


class WorldClass(WorkspaceScopedModel):
    """One declared class in the workspace's minimal ontology (editable)."""

    __tablename__ = "world_class"
    __table_args__ = (UniqueConstraint("workspace_id", "key", name="uq_world_class_workspace_key"),)

    #: The API token, ``^[a-z0-9_]{1,40}$`` (validated by ``forge_world``).
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    disjoint_with: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    iri: Mapped[str | None] = mapped_column(String(512), nullable=True)


class WorldRelation(WorkspaceScopedModel):
    """One declared relation: its kind, domain/range, and reasoning flags."""

    __tablename__ = "world_relation"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_world_relation_workspace_key"),
    )

    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    #: ``state`` | ``event`` | ``eternal``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    domain_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: A class key, or ``value`` for a literal range.
    range_class: Mapped[str | None] = mapped_column(String(40), nullable=True)
    functional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    inverse_functional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    symmetric: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    inverse_of: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sub_property_of: Mapped[str | None] = mapped_column(String(40), nullable=True)
    iri: Mapped[str | None] = mapped_column(String(512), nullable=True)


class WorldEntity(WorkspaceScopedModel):
    """A named (or merely described) thing facts are about."""

    __tablename__ = "world_entity"
    __table_args__ = (
        Index("ix_world_entity_workspace_label", "workspace_id", "label"),
        Index("ix_world_entity_workspace_type", "workspace_id", "type_key"),
    )

    label: Mapped[str] = mapped_column(String(512), nullable=False)
    type_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: ``person`` is final and never overwritten by a model; ``model`` otherwise.
    type_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    described: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    described_in: Mapped[str | None] = mapped_column(String(512), nullable=True)


class WorldFact(WorkspaceScopedModel):
    """One append-only, bitemporal claim (tombstone-only mutation)."""

    __tablename__ = "world_fact"
    __table_args__ = (
        CheckConstraint(
            "(object_id IS NOT NULL AND value IS NULL) "
            "OR (object_id IS NULL AND value IS NOT NULL)",
            name="world_fact_object_xor_value",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="world_fact_confidence_range"),
        CheckConstraint(
            # The explicit IS NOT NULL matters: with a NULL reason a bare
            # ``IN (...)`` is UNKNOWN, and a CHECK passes on UNKNOWN.
            "(invalidated_at IS NULL AND invalidated_reason IS NULL) "
            "OR (invalidated_at IS NOT NULL AND invalidated_reason IS NOT NULL "
            "AND invalidated_reason IN "
            "('superseded','retracted','underived','source_deleted','merged_away'))",
            name="world_fact_tombstone_pair",
        ),
        Index(
            "uq_world_fact_live_dedup",
            "workspace_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("invalidated_at IS NULL"),
            sqlite_where=text("invalidated_at IS NULL"),
        ),
        Index("ix_world_fact_subject", "workspace_id", "subject_id"),
        Index("ix_world_fact_object", "workspace_id", "object_id"),
        Index("ix_world_fact_predicate", "workspace_id", "predicate_key"),
        Index("ix_world_fact_supersedes", "workspace_id", "supersedes_id"),
    )

    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_entity.id", ondelete="CASCADE"), nullable=False
    )
    #: The source phrase, verbatim-ish; the only predicate for ``open`` facts.
    phrase: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The declared relation key for ``typed``/``derived`` facts, else NULL.
    predicate_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_entity.id", ondelete="CASCADE"), nullable=True
    )
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``open`` | ``typed`` | ``derived`` | ``memory``.
    layer: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ``plan`` | ``requirement`` | ``condition``, or NULL for plain claims.
    mood: Mapped[str | None] = mapped_column(String(16), nullable=True)
    qualifiers: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from_precision: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to_precision: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    end_derived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    end_after_period: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: ``document`` | ``person`` | ``agent`` | ``rule`` | ``mcp``.
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_proposal.id", ondelete="CASCADE"), nullable=True
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_fact.id", ondelete="CASCADE"), nullable=True
    )
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``superseded`` | ``retracted`` | ``underived`` | ``source_deleted`` |
    #: ``merged_away``.
    invalidated_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class WorldEvidence(WorkspaceScopedModel):
    """A verbatim quote backing a document fact (append-only)."""

    __tablename__ = "world_evidence"
    __table_args__ = (Index("ix_world_evidence_fact", "workspace_id", "fact_id"),)

    fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_fact.id", ondelete="CASCADE"), nullable=False
    )
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    #: Server-computed offsets of ``quote`` within the chunk's text.
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    doc_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorldExtractionDrop(WorkspaceScopedModel):
    """One rejected extraction item with its coded reason (append-only)."""

    __tablename__ = "world_extraction_drop"
    __table_args__ = (Index("ix_world_drop_source", "workspace_id", "source_ref"),)

    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    item: Mapped[dict[str, Any] | None] = mapped_column(json_type(), nullable=True)


class WorldProposal(WorkspaceScopedModel):
    """A machine-proposed fact awaiting a person's decision."""

    __tablename__ = "world_proposal"
    __table_args__ = (Index("ix_world_proposal_status", "workspace_id", "status"),)

    kind: Mapped[str] = mapped_column(String(16), default="fact", nullable=False)
    #: The proposed statement (name- or id-based), as submitted.
    payload: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    #: ``proposed`` | ``accepted`` | ``rejected`` (transitions exactly once).
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)
    proposer_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    proposer: Mapped[str] = mapped_column(String(255), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: The shadow governance verdict: ``{decision, threshold, reasons}``.
    gate: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "world_fact.id",
            ondelete="CASCADE",
            use_alter=True,
            name="fk_world_proposal_fact_id_world_fact",
        ),
        nullable=True,
    )


class WorldEntityMerge(WorkspaceScopedModel):
    """``merged_id`` was merged into ``survivor_id`` (revert stamps once)."""

    __tablename__ = "world_entity_merge"
    __table_args__ = (
        Index(
            "uq_world_merge_live_merged",
            "workspace_id",
            "merged_id",
            unique=True,
            postgresql_where=text("reverted_at IS NULL"),
            sqlite_where=text("reverted_at IS NULL"),
        ),
        Index("ix_world_merge_survivor", "workspace_id", "survivor_id"),
    )

    survivor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_entity.id", ondelete="CASCADE"), nullable=False
    )
    merged_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_entity.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    revert_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorldDerivation(WorkspaceScopedModel):
    """The proof of a derived fact: the rule and its premises (append-only)."""

    __tablename__ = "world_derivation"
    __table_args__ = (Index("ix_world_derivation_fact", "workspace_id", "fact_id"),)

    fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("world_fact.id", ondelete="CASCADE"), nullable=False
    )
    #: ``symmetric`` | ``inverse`` | ``sub_property`` | ``transitive``.
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    premise_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)


class WorldDecision(WorkspaceScopedModel):
    """A recorded decision and the facts it rested on (append-only)."""

    __tablename__ = "world_decision"

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    outcome: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fact_ids: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    links: Mapped[dict[str, Any]] = mapped_column(json_type(), default=dict, nullable=False)


# Postgres: tombstone-only guard on world_fact; append-only elsewhere.
_attach_tombstone_trigger(WorldFact.__table__)
attach_immutability_trigger(WorldEvidence.__table__)
attach_immutability_trigger(WorldExtractionDrop.__table__)
attach_immutability_trigger(WorldDerivation.__table__)
attach_immutability_trigger(WorldDecision.__table__)


__all__ = [
    "WorldClass",
    "WorldDecision",
    "WorldDerivation",
    "WorldEntity",
    "WorldEntityMerge",
    "WorldEvidence",
    "WorldExtractionDrop",
    "WorldFact",
    "WorldProposal",
    "WorldRelation",
]
