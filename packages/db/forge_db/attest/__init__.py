"""Attested Changesets storage layer: the append-only ``attestation`` table.

The ORM model lives in ``forge_db.models.attestation``; this subpackage adds
the workspace-scoped, insert-only query surface both ``forge_api`` and
``forge_worker`` share — :class:`~forge_db.attest.repository.AttestationRepository`.

Assembling and signing the DSSE envelope (subject digest, PAE encoding,
Ed25519 signature) is a separate slice; this repository only persists and
reads already-built :class:`~forge_db.models.attestation.Attestation` rows.
"""

from __future__ import annotations

from forge_db.attest.repository import AttestationRepository
from forge_db.base import attach_immutability_trigger

__all__ = ["AttestationRepository", "attach_immutability_trigger"]
