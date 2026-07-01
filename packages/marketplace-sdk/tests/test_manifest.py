"""Manifest loading, canonical hashing, and content-hash consistency (AC4b/AC20)."""

from __future__ import annotations

import pytest

from forge_marketplace.errors import SchemaInvalid
from forge_marketplace.manifest import (
    canonical_artifact_bytes,
    compute_content_hash,
    dump_manifest,
    load_manifest,
)


def _valid_manifest_dict() -> dict:
    artifact = {"schema_version": 1, "name": "backend-tdd-strict", "min_test_coverage": 95}
    return {
        "schema_version": 1,
        "kind": "skill_profile",
        "slug": "backend-tdd-strict",
        "name": "Backend TDD",
        "version": "1.2.0",
        "summary": "hardened",
        "artifact": artifact,
        "content_hash": compute_content_hash(artifact),
    }


def test_manifest_loads_valid() -> None:
    manifest = load_manifest(_valid_manifest_dict())
    assert manifest.slug == "backend-tdd-strict"
    assert manifest.version == "1.2.0"


def test_manifest_rejects_extra_key() -> None:
    """AC20: extra='forbid' — an unexpected manifest key fails closed."""
    data = _valid_manifest_dict()
    data["totally_unknown_field"] = "smuggled"
    with pytest.raises(SchemaInvalid):
        load_manifest(data)


def test_manifest_rejects_bad_slug() -> None:
    data = _valid_manifest_dict()
    data["slug"] = "Not A Slug"
    with pytest.raises(SchemaInvalid):
        load_manifest(data)


def test_manifest_rejects_non_semver_version() -> None:
    data = _valid_manifest_dict()
    data["version"] = "v1"
    with pytest.raises(SchemaInvalid):
        load_manifest(data)


def test_manifest_content_hash_consistency() -> None:
    """AC4b: a declared content_hash that disagrees with the artifact is rejected."""
    data = _valid_manifest_dict()
    data["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(SchemaInvalid, match="content_hash"):
        load_manifest(data)


def test_canonical_artifact_bytes_is_deterministic() -> None:
    """AC4/AC20: key order / whitespace do not change the canonical bytes."""
    a = {"b": 2, "a": 1, "nested": {"y": 1, "x": 2}}
    b = {"a": 1, "nested": {"x": 2, "y": 1}, "b": 2}
    assert canonical_artifact_bytes(a) == canonical_artifact_bytes(b)
    assert compute_content_hash(a) == compute_content_hash(b)
    assert compute_content_hash(a).startswith("sha256:")


def test_manifest_yaml_roundtrip() -> None:
    manifest = load_manifest(_valid_manifest_dict())
    reloaded = load_manifest(dump_manifest(manifest))
    assert reloaded.content_hash == manifest.content_hash
    assert reloaded.artifact == manifest.artifact


def test_load_manifest_rejects_non_mapping() -> None:
    with pytest.raises(SchemaInvalid):
        load_manifest("- just\n- a\n- list\n")
