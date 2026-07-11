"""DsseSigner/DsseVerifier: sign -> verify round-trip, tamper, wrong-key (F41 dsse-signer)."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from forge_auth.errors import KeyMaterialError
from forge_contracts.attestation import DSSE_PAYLOAD_TYPE_INTOTO, DsseEnvelope, DsseSignature
from forge_obs.attest.signing import (
    SIGNING_KEY_ENV,
    DsseSigner,
    DsseVerifier,
    EnvSigningKeyProvider,
    pae,
)

PAYLOAD = b'{"hello":"world"}'


def _seed_env(seed: bytes) -> dict[str, str]:
    return {SIGNING_KEY_ENV: base64.b64encode(seed).decode("ascii")}


def _provider(seed: bytes = b"\x01" * 32) -> EnvSigningKeyProvider:
    return EnvSigningKeyProvider(_seed_env(seed))


# --------------------------------------------------------------------------- #
# pae() — the Pre-Authentication Encoding                                      #
# --------------------------------------------------------------------------- #


def test_pae_matches_the_dsse_formula() -> None:
    payload_type = "application/vnd.in-toto+json"
    body = b"body-bytes"
    expected = (
        b"DSSEv1 "
        + str(len(payload_type)).encode("ascii")
        + b" "
        + payload_type.encode("ascii")
        + b" "
        + str(len(body)).encode("ascii")
        + b" "
        + body
    )
    assert pae(payload_type, body) == expected


def test_pae_is_deterministic() -> None:
    assert pae("type-a", b"body") == pae("type-a", b"body")


def test_pae_length_prefixes_prevent_boundary_collisions() -> None:
    # Without length-prefixing, ("ab", "c") and ("a", "bc") would concatenate
    # to the same bytes; PAE's explicit lengths keep them distinct.
    assert pae("ab", b"c") != pae("a", b"bc")


# --------------------------------------------------------------------------- #
# EnvSigningKeyProvider                                                        #
# --------------------------------------------------------------------------- #


def test_provider_derives_keyid_as_sha256_of_raw_public_key() -> None:
    seed = b"\x02" * 32
    provider = _provider(seed)
    expected_public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()

    assert provider.keyid == hashlib.sha256(expected_public).hexdigest()
    assert provider.public_key_b64 == base64.b64encode(expected_public).decode("ascii")


def test_provider_is_deterministic_for_the_same_seed() -> None:
    seed = b"\x05" * 32
    assert _provider(seed).keyid == _provider(seed).keyid


def test_provider_rejects_malformed_base64() -> None:
    with pytest.raises(KeyMaterialError):
        EnvSigningKeyProvider({SIGNING_KEY_ENV: "!!!not-base64!!!"})


def test_provider_rejects_wrong_seed_size() -> None:
    with pytest.raises(KeyMaterialError):
        EnvSigningKeyProvider({SIGNING_KEY_ENV: base64.b64encode(b"too-short").decode("ascii")})


def test_provider_generates_ephemeral_key_with_warning_when_unset() -> None:
    with pytest.warns(UserWarning, match=SIGNING_KEY_ENV):
        provider = EnvSigningKeyProvider({})
    assert len(provider.keyid) == 64  # sha256 hex digest


def test_provider_ephemeral_keys_differ_per_instance() -> None:
    with pytest.warns(UserWarning):
        first = EnvSigningKeyProvider({})
    with pytest.warns(UserWarning):
        second = EnvSigningKeyProvider({})
    assert first.keyid != second.keyid


# --------------------------------------------------------------------------- #
# DsseSigner / DsseVerifier                                                    #
# --------------------------------------------------------------------------- #


def test_sign_then_verify_round_trips() -> None:
    signer = DsseSigner(_provider())
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)

    assert envelope.payloadType == DSSE_PAYLOAD_TYPE_INTOTO
    assert base64.b64decode(envelope.payload) == PAYLOAD
    assert len(envelope.signatures) == 1
    assert envelope.signatures[0].keyid == signer.keyid

    assert DsseVerifier().verify(envelope, public_key_b64=signer.public_key_b64) is True


def test_verify_detects_tampered_payload() -> None:
    signer = DsseSigner(_provider())
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)

    tampered_payload_b64 = base64.b64encode(b'{"hello":"mallory"}').decode("ascii")
    tampered = envelope.model_copy(update={"payload": tampered_payload_b64})

    assert DsseVerifier().verify(tampered, public_key_b64=signer.public_key_b64) is False


def test_verify_detects_tampered_payload_type() -> None:
    """PAE binds payloadType into the signed bytes — relabeling it must fail."""
    signer = DsseSigner(_provider())
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)

    tampered = envelope.model_copy(update={"payloadType": "application/vnd.evil+json"})

    assert DsseVerifier().verify(tampered, public_key_b64=signer.public_key_b64) is False


def test_verify_detects_substituted_signature() -> None:
    signer = DsseSigner(_provider())
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)

    other_signer = DsseSigner(_provider(b"\x09" * 32))
    other_envelope = other_signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)
    tampered = envelope.model_copy(update={"signatures": other_envelope.signatures})

    assert DsseVerifier().verify(tampered, public_key_b64=signer.public_key_b64) is False


def test_verify_rejects_wrong_public_key() -> None:
    signer = DsseSigner(_provider())
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)

    wrong_public_key_b64 = _provider(b"\x03" * 32).public_key_b64
    assert DsseVerifier().verify(envelope, public_key_b64=wrong_public_key_b64) is False


def test_verify_is_total_never_raises_on_malformed_envelope() -> None:
    """Mirrors Ed25519SignatureVerifier: pure and total, never raises."""
    envelope = DsseEnvelope(
        payloadType=DSSE_PAYLOAD_TYPE_INTOTO,
        payload="!!!not-base64!!!",
        signatures=[DsseSignature(keyid="k", sig="also-not-base64")],
    )
    assert DsseVerifier().verify(envelope, public_key_b64="also-bad") is False


def test_verify_returns_false_for_envelope_with_no_signatures() -> None:
    envelope = DsseEnvelope(
        payloadType=DSSE_PAYLOAD_TYPE_INTOTO,
        payload=base64.b64encode(PAYLOAD).decode("ascii"),
    )
    assert DsseVerifier().verify(envelope, public_key_b64=_provider().public_key_b64) is False


def test_default_signer_reads_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seed = b"\x07" * 32
    monkeypatch.setenv(SIGNING_KEY_ENV, base64.b64encode(seed).decode("ascii"))

    signer = DsseSigner()
    envelope = signer.sign(payload_type=DSSE_PAYLOAD_TYPE_INTOTO, payload=PAYLOAD)

    assert DsseVerifier().verify(envelope, public_key_b64=signer.public_key_b64) is True
    assert signer.keyid == _provider(seed).keyid
