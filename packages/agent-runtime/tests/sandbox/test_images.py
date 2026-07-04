"""AC14 — image allowlist enforcement + per-language defaults."""

from __future__ import annotations

import pytest

from forge_agent.sandbox import SandboxImageNotAllowed, SandboxSettings, resolve_image
from forge_contracts import PolicySandboxBlock


def test_per_language_defaults() -> None:
    settings = SandboxSettings()
    assert resolve_image("python", None, settings) == settings.image_python
    assert resolve_image("node", None, settings) == settings.image_node
    assert resolve_image("go", None, settings) == settings.image_go
    # Unknown language falls back to python.
    assert resolve_image("rust", None, settings) == settings.image_python
    assert resolve_image(None, None, settings) == settings.image_python


def test_policy_image_allowed() -> None:
    settings = SandboxSettings()
    allowed = settings.image_node
    block = PolicySandboxBlock(image=allowed)
    assert resolve_image("python", block, settings) == allowed


def test_policy_image_not_allowed_raises() -> None:
    settings = SandboxSettings()
    block = PolicySandboxBlock(image="ghcr.io/attacker/pwn@sha256:deadbeef")
    with pytest.raises(SandboxImageNotAllowed):
        resolve_image("python", block, settings)


def test_explicit_allowlist_overrides_defaults() -> None:
    settings = SandboxSettings(allowed_images=("only/this:1.0",))
    # The per-language default image is no longer on the allowlist, but no policy
    # image was requested so the default is returned as-is (workspace trust).
    assert resolve_image("python", None, settings) == settings.image_python
    # A policy requesting the default (not on the explicit allowlist) is rejected.
    block = PolicySandboxBlock(image=settings.image_python)
    with pytest.raises(SandboxImageNotAllowed):
        resolve_image("python", block, settings)
