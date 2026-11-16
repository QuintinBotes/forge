"""Attested Changesets storage layer: the append-only ``attestation`` table.

The ORM model lives in ``scf_db.models.attestation``; this subpackage adds
the workspace-scoped, insert-only query surface both ``scf_api`` and
``scf_worker`` share — :class:`~scf_db.attest.repository.AttestationRepository`.

Assembling and signing the DSSE envelope (subject digest, PAE encoding,
Ed25519 signature) is a separate slice; this repository only persists and
reads already-built :class:`~scf_db.models.attestation.Attestation` rows.
"""

from __future__ import annotations

from scf_db.attest.repository import AttestationRepository
from scf_db.base import attach_immutability_trigger

__all__ = ["AttestationRepository", "attach_immutability_trigger"]
