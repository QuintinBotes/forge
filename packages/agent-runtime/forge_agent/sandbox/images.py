"""Sandbox image resolution + allowlist enforcement (F19).

A repo's ``policy.sandbox.image`` may pin a specific curated image, but only if it
is a member of the workspace ``FORGE_SANDBOX_ALLOWED_IMAGES`` allowlist — a repo
can never point the sandbox at an arbitrary/malicious image. When no image is
requested, the per-language default from settings is used.
"""

from __future__ import annotations

from forge_agent.sandbox.base import SandboxImageNotAllowed
from forge_agent.sandbox.settings import SandboxSettings
from forge_contracts import PolicySandboxBlock


def resolve_image(
    language: str | None,
    policy_block: PolicySandboxBlock | None,
    settings: SandboxSettings,
) -> str:
    """Return the digest-pinned image to run, enforcing the allowlist.

    Raises:
        SandboxImageNotAllowed: if ``policy_block.image`` is not allow-listed.
    """
    allowed = settings.resolved_allowed_images()
    requested = policy_block.image if policy_block is not None else None
    if requested:
        if requested not in allowed:
            raise SandboxImageNotAllowed(
                f"sandbox image {requested!r} is not on the allowlist ({', '.join(allowed)})"
            )
        return requested
    return settings.image_for(language)


__all__ = ["resolve_image"]
