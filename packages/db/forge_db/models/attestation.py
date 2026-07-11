"""``Attestation`` — an immutable DSSE-signed provenance record over a changeset.

(attestation-table) The storage substrate for "Attested Changesets": one
append-only row per signed attestation, carrying the DSSE envelope (payload +
payload type + signatures) alongside the queryable provenance fields the
dashboard/verify surfaces filter on without re-parsing the envelope — the
Ed25519 ``keyid`` that signed it, the linked ``workflow_run``/``agent_run``,
the PR numbers it covers, and the spec revision it was produced against.

Truthful-provenance sourcing (see the slice's grounded seams): callers attest
what actually ran, never a planned/target value —
``agent_run_id``/``keyid`` come from a concrete signed ``AgentRun``,
``spec_key``/``spec_version`` from a concrete ``SpecVersion.version_number``,
and ``pr_numbers`` from ``TraceabilityCriterionLink.pr_numbers``. This module
is the storage layer only; assembling and signing the DSSE envelope from those
sources is a separate slice.

Append-only at two layers, mirroring ``policy_rule_evaluation``/
``audit_log``: the repository (``forge_db.attest.repository``) exposes no
update/delete path, and the table opts into the shared Postgres
``attach_immutability_trigger`` (BEFORE UPDATE/DELETE -> raise; a no-op on the
SQLite unit path, where the repository's missing mutation surface is the only
guard).

``workflow_run_id``/``agent_run_id`` are nullable ``CASCADE`` FKs, mirroring
``policy_rule_evaluation.agent_run_id`` exactly (not ``audit_log.actor_id``'s
``SET NULL``): a Postgres ``ON DELETE SET NULL`` action is itself an UPDATE of
the referencing row, and ``attach_immutability_trigger`` blocks *every*
UPDATE/DELETE unconditionally — including ones the database issues internally
to enforce a referential action — so ``SET NULL`` would make deleting the
parent row raise instead of silently detaching it. ``CASCADE`` has the same
underlying tension (the resulting DELETE on ``attestation`` is itself blocked),
but this is the accepted, already-shipped convention for run-linked immutable
rows: in practice ``agent_run``/``workflow_run`` rows are never hard-deleted.

``audit_seq`` is the optional pointer back to the F39 ``audit_log`` row this
attestation was chained from (``AuditEvent.detail_ref={"table": "attestation",
"id": ...}`` is the reverse direction); ``merkle_leaf_hash`` is reserved for a
future batch-Merkle-root anchoring scheme and unused by this slice.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from forge_db.base import WorkspaceScopedModel, attach_immutability_trigger, json_type


class Attestation(WorkspaceScopedModel):
    """One immutable DSSE-signed provenance attestation over a changeset."""

    __tablename__ = "attestation"
    __table_args__ = (
        Index("ix_attestation_workspace_created", "workspace_id", "created_at"),
        Index("ix_attestation_subject_digest", "subject_digest"),
        Index("ix_attestation_workflow_run_id", "workflow_run_id"),
        Index("ix_attestation_agent_run_id", "agent_run_id"),
    )

    #: ``sha256:<hex>`` (or similar labeled digest) of the attested subject —
    #: the changeset/artifact the envelope's payload describes.
    subject_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The in-toto-style predicate type URI (e.g.
    #: ``https://forge.dev/attestations/changeset/v1``).
    predicate_type: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The full DSSE envelope: ``{"payload", "payloadType", "signatures"}``.
    envelope: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False)
    #: sha256 hex of the canonical (PAE-encoded) payload the signature covers.
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Identifier of the versioned Ed25519 key that produced the signature.
    keyid: Mapped[str] = mapped_column(String(128), nullable=False)

    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_run.id", ondelete="CASCADE"), nullable=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=True
    )
    #: PR numbers this attestation covers (mirrors
    #: ``TraceabilityCriterionLink.pr_numbers``).
    pr_numbers: Mapped[list[Any]] = mapped_column(json_type(), default=list, nullable=False)
    #: The spec this changeset was produced against (``SpecVersion.spec_key`` /
    #: ``.version_number``); nullable — not every attested changeset has one.
    spec_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spec_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Chain position of the F39 ``audit_log`` row this attestation was
    #: recorded alongside (per-workspace ``AuditLog.seq``); NULL until wired.
    audit_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Reserved for a future batch-Merkle-root anchoring scheme; unused here.
    merkle_leaf_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


# Postgres: append-only via the shared BEFORE UPDATE/DELETE trigger.
attach_immutability_trigger(Attestation.__table__)


__all__ = ["Attestation"]
