"""Hard enforcement of ``policy.skill_profiles.allowed`` (F40-POL-GOVERNANCE).

The ``PolicySkillProfiles`` block (``.forge/policy.yaml``) already carries a
``default`` profile and an ``allowed`` whitelist, but nothing read the whitelist:
a run could request any skill profile regardless of policy. This module closes
that gap with a pure, total check the run-creation path invokes before a run is
admitted (the API maps :class:`SkillProfileNotAllowedError` to HTTP 422).

Semantics (fail-closed, but a repo that declares no whitelist is unconstrained):

* the ``default`` profile is always implicitly allowed;
* an empty ``allowed`` list (and no ``default``) means "no profile restriction
  declared" — any profile, or none, is admitted;
* a requested profile outside a non-empty allow-set is rejected;
* ``requested is None`` selects the ``default`` and is always admitted.

Evaluation is a pure function over the frozen :class:`Policy` DTO — no I/O.
"""

from __future__ import annotations

from forge_contracts import ForgeError, Policy

__all__ = [
    "SkillProfileNotAllowedError",
    "allowed_skill_profiles",
    "enforce_skill_profile_allowed",
    "is_skill_profile_allowed",
]


class SkillProfileNotAllowedError(ForgeError):
    """A run requested a skill profile the repo policy does not allow.

    Carries the offending ``profile`` and the ``allowed`` set so the API layer
    can render an actionable 422 without re-deriving them.
    """

    def __init__(self, profile: str, allowed: frozenset[str]) -> None:
        self.profile = profile
        self.allowed = allowed
        allowed_str = ", ".join(sorted(allowed)) or "(none)"
        super().__init__(
            f"skill profile {profile!r} is not allowed by policy; allowed profiles: {allowed_str}"
        )


def allowed_skill_profiles(policy: Policy) -> frozenset[str]:
    """Return the effective allow-set (the whitelist plus the default profile)."""
    profiles = policy.skill_profiles
    names = set(profiles.allowed)
    if profiles.default:
        names.add(profiles.default)
    return frozenset(names)


def is_skill_profile_allowed(policy: Policy, requested: str | None) -> bool:
    """True if ``requested`` may run under ``policy`` (``None`` -> the default)."""
    allowed = allowed_skill_profiles(policy)
    if not allowed:
        # No whitelist declared: the repo places no skill-profile restriction.
        return True
    if requested is None:
        # An unspecified profile falls back to the (always-allowed) default.
        return True
    return requested in allowed


def enforce_skill_profile_allowed(policy: Policy, requested: str | None) -> None:
    """Raise :class:`SkillProfileNotAllowedError` unless ``requested`` is allowed."""
    if not is_skill_profile_allowed(policy, requested):
        assert requested is not None  # a None request is always allowed above
        raise SkillProfileNotAllowedError(requested, allowed_skill_profiles(policy))
