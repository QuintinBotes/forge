"""Keep secrets out of Temporal's durable event history (F25).

Temporal persists every Workflow/Activity input and output in its event history
*at rest*. :class:`RedactingEncryptionCodec` is the defence-in-depth
:class:`~temporalio.converter.PayloadCodec` registered on both the client and the
worker:

* ``encode`` runs a canonical secret redactor over each payload's data **then**
  AES-GCM encrypts it with a key derived from ``TEMPORAL_CODEC_KEY`` — so the raw
  history (and the Temporal Web UI, absent a trusted codec server) only ever sees
  ciphertext, and even a decrypted payload is redacted.
* ``decode`` decrypts and returns the redacted-but-functional payload. A wrong
  key fails closed (the AES-GCM tag check raises).

The redaction patterns mirror the canonical :mod:`forge_knowledge.redaction`
(AWS keys, PEM private-key blocks, bearer tokens, ``key=value`` secrets, JWTs);
they are inlined here to keep ``forge_workflow`` free of the heavier
knowledge-core dependency closure (tree-sitter / numpy).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

#: Placeholder substituted for any redacted secret (matches forge_mcp.security).
REDACTED = "[redacted]"

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|authorization|"
    r"aws_secret_access_key|aws_access_key_id|private_key|client_secret)\b"
    r"(\\?[\"']?\s*[:=]\s*\\?[\"']?)[^\s\"',}]+"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

#: Metadata encoding marker for our codec-wrapped payloads.
_ENCODING = b"binary/encrypted"
_NONCE_LEN = 12


def redact_secrets(text: str) -> str:
    """Return ``text`` with secrets masked. Idempotent and never raises."""
    if not text:
        return text
    text = _PEM_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub("Bearer " + REDACTED, text)
    text = _KV_SECRET_RE.sub(lambda m: _redact_kv(m), text)
    text = _JWT_RE.sub(REDACTED, text)
    text = _AWS_KEY_RE.sub(REDACTED, text)
    return text


def _redact_kv(match: re.Match[str]) -> str:
    # Preserve the key + separator (group 1), mask the value.
    return f"{match.group(0)[: match.start(1) - match.start(0)]}{match.group(1)}{REDACTED}"


def derive_key(key_material: str | bytes) -> bytes:
    """Derive a 32-byte AES-256 key from arbitrary key material (SHA-256)."""
    if isinstance(key_material, str):
        key_material = key_material.encode("utf-8")
    if not key_material:
        raise ValueError("TEMPORAL_CODEC_KEY must be set when the temporal backend is selected")
    return hashlib.sha256(key_material).digest()


class RedactingEncryptionCodec(PayloadCodec):
    """Redact-then-AES-GCM-encrypt PayloadCodec for Temporal (F25)."""

    def __init__(self, key_material: str | bytes) -> None:
        self._aesgcm = AESGCM(derive_key(key_material))

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._encode_one(p) for p in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._decode_one(p) for p in payloads]

    # -- internals --------------------------------------------------------- #

    def _encode_one(self, payload: Payload) -> Payload:
        redacted = Payload(
            metadata=dict(payload.metadata),
            data=redact_secrets(payload.data.decode("utf-8", errors="replace")).encode("utf-8")
            if _is_text(payload)
            else payload.data,
        )
        ciphertext = self._encrypt(redacted.SerializeToString())
        return Payload(metadata={"encoding": _ENCODING}, data=ciphertext)

    def _decode_one(self, payload: Payload) -> Payload:
        if payload.metadata.get("encoding") != _ENCODING:
            return payload
        plaintext = self._decrypt(payload.data)
        return Payload.FromString(plaintext)

    def _encrypt(self, data: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aesgcm.encrypt(nonce, data, None)

    def _decrypt(self, data: bytes) -> bytes:
        try:
            return self._aesgcm.decrypt(data[:_NONCE_LEN], data[_NONCE_LEN:], None)
        except InvalidTag as exc:  # fail closed on a wrong/rotated key
            raise ValueError("temporal codec: payload decryption failed (bad key)") from exc


def _is_text(payload: Payload) -> bool:
    encoding = payload.metadata.get("encoding", b"")
    return encoding.startswith(b"json") or encoding.startswith(b"text")


__all__ = ["REDACTED", "RedactingEncryptionCodec", "derive_key", "redact_secrets"]
