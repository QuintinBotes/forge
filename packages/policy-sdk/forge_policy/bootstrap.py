"""Bootstrap a repo's ``.forge/policy.yaml`` from a named policy profile (F40).

A repo onboarding onto Forge needs a starting ``.forge/policy.yaml``. This module
turns a small set of curated :class:`~forge_contracts.Policy` *profiles* (a safe
default, a locked-down profile, a permissive dev profile) into an on-disk policy
file. Serialization round-trips through the frozen ``Policy`` DTO, so a
bootstrapped file always re-loads (:func:`forge_policy.load_policy`) byte-for-byte
into the same policy — the bootstrap can never emit a file the loader rejects.

Bootstrapping never clobbers an existing policy unless ``overwrite=True`` — a repo
that already declares governance keeps it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from forge_contracts import (
    DeployRules,
    Policy,
    PolicySkillProfiles,
    ReviewRules,
    WriteRules,
)
from forge_policy.loader import POLICY_RELATIVE_PATH

__all__ = [
    "POLICY_PROFILES",
    "PolicyBootstrapError",
    "bootstrap_policy_file",
    "policy_profile",
]


class PolicyBootstrapError(Exception):
    """Raised when a policy cannot be bootstrapped (unknown profile / conflict)."""


def _default_profile(repo_id: str) -> Policy:
    """The recommended starting policy: human merge gate, no agent deploys."""
    return Policy(
        repo_id=repo_id,
        name=repo_id,
        write_rules=WriteRules(
            allow=["src/**", "app/**", "tests/**", "docs/**"],
            deny=[".env*", "secrets/**", "*.pem", "*.key", "infra/prod/**"],
        ),
        review_rules=ReviewRules(approval_required_for_merge=True, min_approvals=1),
        deploy_rules=DeployRules(
            allow_agent_deploy=False,
            environments=["dev"],
            restricted_environments=["staging", "production"],
        ),
        skill_profiles=PolicySkillProfiles(default="backend-tdd", allowed=["backend-tdd"]),
    )


def _locked_profile(repo_id: str) -> Policy:
    """A hardened profile: two approvals, tighter writes, no agent deploys."""
    return Policy(
        repo_id=repo_id,
        name=repo_id,
        write_rules=WriteRules(
            allow=["src/**", "tests/**"],
            deny=[".env*", "secrets/**", "*.pem", "*.key", "infra/**", "**/migrations/**"],
        ),
        review_rules=ReviewRules(approval_required_for_merge=True, min_approvals=2),
        deploy_rules=DeployRules(
            allow_agent_deploy=False,
            environments=[],
            restricted_environments=["dev", "staging", "production"],
        ),
        skill_profiles=PolicySkillProfiles(default="security-review", allowed=["security-review"]),
    )


def _dev_profile(repo_id: str) -> Policy:
    """A permissive profile for a throwaway/dev repo (still human-gated merge)."""
    return Policy(
        repo_id=repo_id,
        name=repo_id,
        write_rules=WriteRules(allow=["**"], deny=[".env*", "secrets/**"]),
        review_rules=ReviewRules(approval_required_for_merge=True, min_approvals=1),
        deploy_rules=DeployRules(allow_agent_deploy=True, environments=["dev"]),
    )


#: The curated bootstrap profiles, by name.
POLICY_PROFILES = {
    "default": _default_profile,
    "locked": _locked_profile,
    "dev": _dev_profile,
}


def policy_profile(name: str, repo_id: str) -> Policy:
    """Return the named starter :class:`Policy` for ``repo_id``."""
    builder = POLICY_PROFILES.get(name)
    if builder is None:
        known = ", ".join(sorted(POLICY_PROFILES))
        raise PolicyBootstrapError(f"unknown policy profile {name!r}; known profiles: {known}")
    return builder(repo_id)


def _policy_to_yaml(policy: Policy) -> str:
    """Serialize ``policy`` to a stable YAML document (defaults pruned)."""
    data = policy.model_dump(exclude_defaults=True, exclude_none=True, mode="json")
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False)


def bootstrap_policy_file(
    repo_root: str | Path,
    *,
    profile: str = "default",
    repo_id: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Write ``<repo_root>/.forge/policy.yaml`` from the named ``profile``.

    Returns the written path. Raises :class:`PolicyBootstrapError` when a policy
    already exists and ``overwrite`` is ``False`` (a repo's own governance is
    never silently replaced).
    """
    root = Path(repo_root)
    target = root / POLICY_RELATIVE_PATH
    if target.exists() and not overwrite:
        raise PolicyBootstrapError(f"policy already exists at {target} (pass overwrite=True)")

    policy = policy_profile(profile, repo_id or root.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_policy_to_yaml(policy), encoding="utf-8")
    return target
