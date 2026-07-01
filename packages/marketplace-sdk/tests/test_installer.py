"""Installer validation + the least-privilege security floor (AC7/AC8/AC9/AC20)."""

from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest
from _mp_helpers import Package

from forge_marketplace import installer as installer_mod
from forge_marketplace.errors import SchemaInvalid, UnknownArtifactKind
from forge_marketplace.installer import (
    McpConnectorInstaller,
    SkillProfileInstaller,
    build_install_plan,
    get_installer,
)
from forge_marketplace.models import ArtifactKind, VerificationResult, VerificationStatus

_VERIFIED = VerificationResult(
    status=VerificationStatus.verified, content_hash_ok=True, signature_ok=True
)


def test_skill_installer_validates_and_blocks_bad_profile() -> None:
    """AC7/AC9: a valid profile validates; a type-invalid one is blocked."""
    good = SkillProfileInstaller().validate(
        {"name": "custom-x", "min_test_coverage": 90, "verification_steps": ["lint"]}
    )
    assert good.target_kind == "skill_profile"
    assert good.resolved_config["name"] == "custom-x"

    with pytest.raises(SchemaInvalid):
        # verification_steps must be a list, not an int -> F11 loader rejects.
        SkillProfileInstaller().validate({"name": "bad", "verification_steps": 5})


def test_skill_installer_flags_builtin_override() -> None:
    """AC9: a name shadowing a builtin becomes an override (warning, not block)."""
    result = SkillProfileInstaller().validate({"name": "backend-tdd"})
    assert result.overrides_builtin is True
    assert any("shadows a builtin" in w for w in result.warnings)


def test_mcp_installer_forces_readonly() -> None:
    """AC8: allow_write is forced false regardless of the declared value."""
    result = McpConnectorInstaller().validate(
        {
            "id": "c",
            "name": "C",
            "transport": "http",
            "allow_write": True,
            "allowed_namespaces": ["confluence"],
        }
    )
    assert result.resolved_config["allow_write"] is False
    assert any("allow_write" in w for w in result.warnings)
    assert result.requires_admin_followup  # connect/credentials guidance present


def test_mcp_installer_blocks_stdio() -> None:
    """AC8: stdio transport is rejected (matches F09 V1 limits)."""
    with pytest.raises(SchemaInvalid, match="stdio"):
        McpConnectorInstaller().validate(
            {"id": "c", "name": "C", "transport": "stdio", "allowed_namespaces": ["x"]}
        )


def test_mcp_installer_warns_empty_namespaces() -> None:
    result = McpConnectorInstaller().validate(
        {"id": "c", "name": "C", "transport": "http", "allowed_namespaces": []}
    )
    assert any("over-broad" in w for w in result.warnings)


def test_reserved_kinds_have_no_installer() -> None:
    """§12: workflow/policy template kinds are reserved -> no installer registered."""
    with pytest.raises(UnknownArtifactKind):
        get_installer(ArtifactKind.workflow_template)
    with pytest.raises(UnknownArtifactKind):
        get_installer(ArtifactKind.policy_template)


def test_no_dynamic_execution_in_installer() -> None:
    """AC20: the installer has no code-execution path."""
    src = inspect.getsource(installer_mod)
    for forbidden in ("eval(", "exec(", "__import__(", "importlib", "compile("):
        assert forbidden not in src, f"installer must not contain {forbidden!r}"


def test_build_install_plan_verified(make_mcp_package: Callable[..., Package]) -> None:
    pkg = make_mcp_package()
    plan = build_install_plan(
        manifest=pkg.manifest,
        version=pkg.manifest.version,
        verification=_VERIFIED,
        forge_version="3.0.0",
    )
    assert plan.blocked is False
    assert plan.resolved_config["allow_write"] is False
    assert plan.requires_admin_followup


def test_build_install_plan_blocks_on_verification(
    make_skill_package: Callable[..., Package],
) -> None:
    pkg = make_skill_package()
    blocked = VerificationResult(
        status=VerificationStatus.hash_mismatch, content_hash_ok=False
    )
    plan = build_install_plan(
        manifest=pkg.manifest,
        version=pkg.manifest.version,
        verification=blocked,
        forge_version="3.0.0",
    )
    assert plan.blocked is True
    assert plan.block_reason


def test_build_install_plan_forge_incompatible(
    make_skill_package: Callable[..., Package],
) -> None:
    """AC14: a min_forge_version above the running Forge blocks the plan."""
    pkg = make_skill_package(min_forge_version="9.9.9")
    plan = build_install_plan(
        manifest=pkg.manifest,
        version=pkg.manifest.version,
        verification=_VERIFIED,
        forge_version="3.0.0",
    )
    assert plan.blocked is True
    assert "requires Forge" in (plan.block_reason or "")


def test_build_install_plan_override_name(make_skill_package: Callable[..., Package]) -> None:
    pkg = make_skill_package()
    plan = build_install_plan(
        manifest=pkg.manifest,
        version=pkg.manifest.version,
        verification=_VERIFIED,
        forge_version="3.0.0",
        override_name="renamed-profile",
    )
    assert plan.resolved_config["name"] == "renamed-profile"
