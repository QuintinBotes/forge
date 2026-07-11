"""Attested Changesets: DSSE signing/verification (F41 slice ``dsse-signer``)."""

from __future__ import annotations

from forge_obs.attest.signing import (
    SIGNING_KEY_ENV,
    DsseSigner,
    DsseVerifier,
    EnvSigningKeyProvider,
    pae,
)

__all__ = [
    "SIGNING_KEY_ENV",
    "DsseSigner",
    "DsseVerifier",
    "EnvSigningKeyProvider",
    "pae",
]
