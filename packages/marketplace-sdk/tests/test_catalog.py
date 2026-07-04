"""Semver compare, compatibility gate, latest/update resolution (AC13/14/15)."""

from __future__ import annotations

from datetime import UTC, datetime

from forge_marketplace.catalog import (
    compare_semver,
    has_newer_compatible,
    is_compatible,
    latest_compatible,
    parse_semver,
)
from forge_marketplace.models import RegistryIndexVersion


def _v(version: str, *, yanked: bool = False, min_forge: str | None = None) -> RegistryIndexVersion:
    return RegistryIndexVersion(
        version=version,
        content_hash="sha256:" + "0" * 64,
        manifest_hash="sha256:" + "0" * 64,
        manifest_uri="x",
        min_forge_version=min_forge,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        yanked=yanked,
    )


def test_compare_semver_ordering() -> None:
    assert compare_semver("1.2.0", "1.10.0") < 0
    assert compare_semver("2.0.0", "1.9.9") > 0
    assert compare_semver("1.0.0", "1.0.0") == 0
    # prerelease sorts below its release
    assert compare_semver("1.2.0-rc.1", "1.2.0") < 0
    assert compare_semver("1.2.0-rc.2", "1.2.0-rc.10") < 0


def test_parse_semver_rejects_non_semver() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_semver("1.2")


def test_is_compatible() -> None:
    assert is_compatible(None, "3.0.0") is True
    assert is_compatible("3.0.0", "3.1.0") is True
    assert is_compatible("3.2.0", "3.1.0") is False


def test_latest_compatible_excludes_yanked_and_incompatible() -> None:
    """AC14/AC15: yanked + too-new-min-forge versions are excluded from latest."""
    versions = [
        _v("1.0.0"),
        _v("1.1.0"),
        _v("1.2.0", yanked=True),  # excluded
        _v("2.0.0", min_forge="9.0.0"),  # excluded (incompatible)
    ]
    latest = latest_compatible(versions, forge_version="3.0.0")
    assert latest is not None
    assert latest.version == "1.1.0"


def test_has_newer_compatible() -> None:
    versions = [_v("1.0.0"), _v("1.1.0"), _v("1.2.0")]
    newer = has_newer_compatible(
        installed_version="1.1.0", versions=versions, forge_version="3.0.0"
    )
    assert newer is not None and newer.version == "1.2.0"
    assert (
        has_newer_compatible(
            installed_version="1.2.0", versions=versions, forge_version="3.0.0"
        )
        is None
    )
