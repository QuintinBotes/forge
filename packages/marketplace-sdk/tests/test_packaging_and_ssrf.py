"""Author packaging round-trip (AC20) + SSRF fetch guard (AC17)."""

from __future__ import annotations

import pytest

from forge_marketplace.errors import SchemaInvalid, SsrfBlocked
from forge_marketplace.manifest import dump_manifest, load_manifest
from forge_marketplace.models import ArtifactKind
from forge_marketplace.packaging import build_package
from forge_marketplace.registry_client import guard_url


def test_build_package_content_hash_reverifies() -> None:
    """AC20: forge marketplace package emits a manifest whose hash re-verifies."""
    manifest = build_package(
        kind=ArtifactKind.skill_profile,
        artifact={"name": "backend-tdd-strict", "min_test_coverage": 95},
        slug="backend-tdd-strict",
        name="Backend TDD",
        version="1.2.0",
        summary="hardened",
    )
    # load_manifest recomputes + asserts the content_hash — no exception == verified.
    reloaded = load_manifest(dump_manifest(manifest))
    assert reloaded.content_hash == manifest.content_hash


def test_build_package_validates_artifact() -> None:
    with pytest.raises(SchemaInvalid):
        build_package(
            kind=ArtifactKind.mcp_connector,
            artifact={"id": "c", "name": "C", "transport": "stdio"},
            slug="bad",
            name="Bad",
            version="1.0.0",
            summary="x",
        )


# --- SSRF guard (AC17) ------------------------------------------------------ #


def _resolver_to(ip: str):
    def _fake(host, port, *args, **kwargs):
        family = 10 if ":" in ip else 2
        return [(family, 1, 6, "", (ip, port))]

    return _fake


def test_guard_blocks_private_address() -> None:
    with pytest.raises(SsrfBlocked):
        guard_url("https://evil.example.com/index.json", resolver=_resolver_to("10.0.0.5"))


def test_guard_blocks_loopback() -> None:
    with pytest.raises(SsrfBlocked):
        guard_url("https://localhost.evil/index.json", resolver=_resolver_to("127.0.0.1"))


def test_guard_blocks_metadata_endpoint() -> None:
    with pytest.raises(SsrfBlocked):
        guard_url("https://metadata/index.json", resolver=_resolver_to("169.254.169.254"))


def test_guard_blocks_ipv6_loopback() -> None:
    with pytest.raises(SsrfBlocked):
        guard_url("https://v6.evil/index.json", resolver=_resolver_to("::1"))


def test_guard_allows_public_address() -> None:
    # Should not raise for a public IP.
    guard_url("https://marketplace.forge.dev/index.json", resolver=_resolver_to("93.184.216.34"))


def test_guard_allowlist_overrides_block() -> None:
    """AC17: an allowlisted host bypasses the private-range guard."""
    guard_url(
        "https://internal-registry.local/index.json",
        allowed_hosts={"internal-registry.local"},
        resolver=_resolver_to("10.0.0.5"),
    )


def test_guard_allows_file_scheme() -> None:
    # Air-gapped official registry via file:// — no network, no SSRF surface.
    guard_url("file:///srv/registry/index.json")


def test_guard_rejects_unsupported_scheme() -> None:
    with pytest.raises(SsrfBlocked):
        guard_url("ftp://example.com/index.json")
