"""Pure-unit tests for the RedactingEncryptionCodec (F25 AC14)."""

from __future__ import annotations

import pytest
from temporalio.api.common.v1 import Payload

from forge_workflow.temporal.converter import REDACTED, RedactingEncryptionCodec

KEY = "unit-test-codec-key"
FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"
FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
)


def _payload(text: str) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=text.encode("utf-8"))


@pytest.mark.asyncio
async def test_encode_redacts_then_encrypts_no_plaintext_secret() -> None:
    codec = RedactingEncryptionCodec(KEY)
    secret_doc = f'{{"aws": "{FAKE_AWS}", "pem": "{FAKE_PEM}", "note": "ok"}}'
    [encoded] = await codec.encode([_payload(secret_doc)])

    # Ciphertext: the secret must not appear in plaintext, nor the field value.
    assert FAKE_AWS.encode() not in encoded.data
    assert b"PRIVATE KEY" not in encoded.data
    assert encoded.metadata.get("encoding") == b"binary/encrypted"


@pytest.mark.asyncio
async def test_decode_round_trips_redacted_but_functional() -> None:
    codec = RedactingEncryptionCodec(KEY)
    secret_doc = f'{{"aws": "{FAKE_AWS}", "note": "keep-me"}}'
    encoded = await codec.encode([_payload(secret_doc)])
    [decoded] = await codec.decode(encoded)

    text = decoded.data.decode("utf-8")
    assert FAKE_AWS not in text  # secret stays redacted after decryption
    assert REDACTED in text
    assert "keep-me" in text  # non-secret content preserved
    assert decoded.metadata.get("encoding") == b"json/plain"


@pytest.mark.asyncio
async def test_wrong_key_fails_closed() -> None:
    encoded = await RedactingEncryptionCodec(KEY).encode([_payload('{"x": "y"}')])
    with pytest.raises(ValueError, match="decryption failed"):
        await RedactingEncryptionCodec("a-different-key").decode(encoded)


@pytest.mark.asyncio
async def test_non_codec_payload_passes_through_decode() -> None:
    plain = _payload('{"x": "y"}')
    [decoded] = await RedactingEncryptionCodec(KEY).decode([plain])
    assert decoded.data == plain.data
