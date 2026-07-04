"""Registry index parsing (AC3) + committed JSON schema drift guard (AC20)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_marketplace.errors import RegistryFetchError
from forge_marketplace.index import parse_index
from forge_marketplace.models import PackageManifest

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "forge_marketplace"
    / "package-manifest.schema.json"
)


def _valid_index() -> dict:
    return {
        "schema_version": 1,
        "registry_name": "Forge Official Marketplace",
        "public_key": None,
        "generated_at": "2026-06-26T00:00:00Z",
        "entries": [
            {
                "kind": "skill_profile",
                "slug": "backend-tdd-strict",
                "name": "Backend TDD (strict)",
                "summary": "Hardened backend-tdd profile",
                "tags": ["backend", "tdd"],
                "license": "Apache-2.0",
                "latest_version": "1.2.0",
                "versions": [
                    {
                        "version": "1.2.0",
                        "content_hash": "sha256:" + "0" * 64,
                        "manifest_hash": "sha256:" + "1" * 64,
                        "signature": None,
                        "manifest_uri": "skill_profile/backend-tdd-strict/1.2.0/forge-package.yaml",
                        "min_forge_version": "3.0.0",
                        "published_at": "2026-06-20T00:00:00Z",
                        "yanked": False,
                    }
                ],
            }
        ],
    }


def test_index_parses_valid() -> None:
    index = parse_index(_valid_index())
    assert index.registry_name.startswith("Forge")
    assert index.entries[0].slug == "backend-tdd-strict"


def test_index_parse_rejects_malformed_extra_key() -> None:
    """AC3: an index with an unexpected top-level key fails closed."""
    data = _valid_index()
    data["unexpected_top_level"] = True
    with pytest.raises(RegistryFetchError):
        parse_index(data)


def test_index_parse_rejects_non_object() -> None:
    with pytest.raises(RegistryFetchError):
        parse_index("[1, 2, 3]")


def test_committed_json_schema_matches_model() -> None:
    """AC20: the committed package-manifest.schema.json equals the generated one."""
    committed = json.loads(_SCHEMA_PATH.read_text())
    generated = PackageManifest.model_json_schema()
    assert committed == generated, (
        "package-manifest.schema.json is stale — regenerate from "
        "PackageManifest.model_json_schema()"
    )
