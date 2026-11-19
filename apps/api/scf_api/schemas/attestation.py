"""Response schemas for the Attested Changesets read-only REST surface
(``GET /attestations``, ``GET /attestations/{id}``,
``GET /approvals/{id}/attestation`` — Task 19).

Field mapping is truthful to ``scf_db.models.attestation.Attestation``:
``changeset_hash`` is the row's ``subject_digest`` (the labeled sha256 of the
attested subject), ``keyid``/``predicate_type``/``payload_hash``/``created_at``
are the columns verbatim, and ``provenance`` carries the queryable provenance
columns the dashboard filters on. ``verified`` is **computed, never stored**:
the router runs the exact ``scf-verify --run`` verification seam
(:func:`scf_api.cli_verify.verify_stored_attestation` — PAE re-derivation +
Ed25519 over the envelope) against the deployment's verification key, so a
record this deployment cannot vouch for honestly reads ``verified: false``.

The raw DSSE ``envelope`` is not part of these records: ``GET
/attestations/{id}/envelope`` serves it exactly as stored, as the contract's
:class:`~scf_contracts.attestation.DsseEnvelope`, for ``scf-verify --attestation``
and other offline verifiers (#214).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AttestationListResponse", "AttestationOut", "AttestationProvenance"]


class AttestationProvenance(BaseModel):
    """The queryable provenance columns of one ``attestation`` row."""

    model_config = ConfigDict(from_attributes=True)

    workflow_run_id: uuid.UUID | None = None
    agent_run_id: uuid.UUID | None = None
    #: PR numbers this attestation covers (mirrors
    #: ``TraceabilityCriterionLink.pr_numbers``).
    pr_numbers: list[int] = Field(default_factory=list)
    #: Spec identity the changeset was produced against; the service degrades
    #: honestly to ``""`` / ``0`` when no traceability exists.
    spec_key: str | None = None
    spec_version: int | None = None
    #: Position of the chained ``changeset.attested`` F39 audit event.
    audit_seq: int | None = None


class AttestationOut(BaseModel):
    """One immutable DSSE-signed changeset attestation, with live verification."""

    id: uuid.UUID
    #: The attested subject's labeled digest (``Attestation.subject_digest``,
    #: ``sha256:<hex>``) — the changeset identity the envelope signs over.
    changeset_hash: str
    #: The in-toto predicate type URI of the signed Statement.
    predicate_type: str
    #: sha256 of the raw Ed25519 public key that signed the envelope.
    keyid: str
    #: sha256 hex of the canonical (PAE-encoded) payload the signature covers.
    payload_hash: str
    created_at: datetime
    #: Computed by the same seam ``scf-verify --run`` uses: recorded
    #: ``payload_hash`` matches the PAE re-derivation AND the Ed25519 signature
    #: verifies against this deployment's verification key.
    verified: bool
    provenance: AttestationProvenance


class AttestationListResponse(BaseModel):
    """Body of ``GET /attestations`` — one workspace-scoped page, newest first."""

    items: list[AttestationOut] = Field(default_factory=list)
    limit: int
    offset: int
