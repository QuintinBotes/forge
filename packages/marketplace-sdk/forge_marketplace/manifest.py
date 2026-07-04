"""Canonical artifact encoding, content hashing, and the fail-closed loader.

The **one** canonical encoder (:func:`canonical_artifact_bytes`) is shared by the
producer (``forge marketplace package``) and the consumer (verifier / install),
so the same logical artifact hashes identically across producers regardless of
key order or whitespace (F32 §9 canonicalization risk).

:func:`load_manifest` parses a ``forge-package.yaml`` fail-closed and *recomputes*
``content_hash`` from the embedded artifact, rejecting any manifest whose declared
hash disagrees (AC4b) — internal-consistency before the registry even asserts a
hash of its own.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml
from pydantic import ValidationError

from forge_marketplace.errors import SchemaInvalid
from forge_marketplace.models import PackageManifest


def canonical_artifact_bytes(artifact: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding of an artifact body.

    Sorted keys, compact separators, UTF-8, ``ensure_ascii=False`` so the byte
    stream is stable across producers. This is the exact payload ``content_hash``
    is computed over on both sides of the trust boundary.
    """
    return json.dumps(
        artifact,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_content_hash(artifact: dict[str, Any]) -> str:
    """Return ``sha256:<hex>`` over :func:`canonical_artifact_bytes`."""
    digest = hashlib.sha256(canonical_artifact_bytes(artifact)).hexdigest()
    return f"sha256:{digest}"


def sha256_hex(payload: bytes) -> str:
    """Return ``sha256:<hex>`` over raw bytes (used for artifact-fetch checks)."""
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def canonical_manifest_bytes(manifest: PackageManifest) -> bytes:
    """Deterministic JSON encoding of the full manifest (the *signed* payload).

    ``manifest_hash`` — the value a registry signs per version — is computed over
    this. ``mode='json'`` so datetimes/enums serialize to their wire form.
    """
    payload = manifest.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_manifest_hash(manifest: PackageManifest) -> str:
    """Return ``sha256:<hex>`` over :func:`canonical_manifest_bytes`."""
    return sha256_hex(canonical_manifest_bytes(manifest))


def load_manifest(raw: str | dict[str, Any]) -> PackageManifest:
    """Parse YAML/JSON (or a mapping) into a :class:`PackageManifest`, fail-closed.

    Recomputes ``content_hash`` from the embedded artifact and asserts it matches
    the declared value (AC4b). Any schema violation or hash disagreement raises
    :class:`SchemaInvalid` — install blocked.
    """
    if isinstance(raw, str):
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise SchemaInvalid(f"manifest is not valid YAML/JSON: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise SchemaInvalid("manifest must be a mapping")

    try:
        manifest = PackageManifest.model_validate(data)
    except ValidationError as exc:
        raise SchemaInvalid(f"manifest failed schema validation: {exc}") from exc

    recomputed = compute_content_hash(manifest.artifact)
    if recomputed != manifest.content_hash:
        raise SchemaInvalid(
            "declared content_hash does not match the embedded artifact "
            f"(declared={manifest.content_hash}, recomputed={recomputed})"
        )
    return manifest


def dump_manifest(manifest: PackageManifest) -> str:
    """Serialize a manifest to a stable YAML document (author-side / CLI output)."""
    payload = manifest.model_dump(mode="json", exclude_none=False)
    return yaml.safe_dump(payload, sort_keys=True, default_flow_style=False, allow_unicode=True)


__all__ = [
    "canonical_artifact_bytes",
    "canonical_manifest_bytes",
    "compute_content_hash",
    "compute_manifest_hash",
    "dump_manifest",
    "load_manifest",
    "sha256_hex",
]
