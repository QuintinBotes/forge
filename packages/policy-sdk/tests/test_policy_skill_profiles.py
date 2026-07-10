"""F40-POL-GOVERNANCE — ``policy.skill_profiles.allowed`` hard enforcement."""

from __future__ import annotations

import pytest

from forge_contracts import Policy
from forge_policy import (
    SkillProfileNotAllowedError,
    allowed_skill_profiles,
    enforce_skill_profile_allowed,
    is_skill_profile_allowed,
)


def _policy(**skill_profiles: object) -> Policy:
    return Policy(repo_id="demo", skill_profiles=skill_profiles)  # type: ignore[arg-type]


def test_allowed_set_includes_default(spec_policy: Policy) -> None:
    allowed = allowed_skill_profiles(spec_policy)
    assert "backend-tdd" in allowed  # the default
    assert "security-review" in allowed  # a whitelisted profile


def test_requested_profile_in_whitelist_is_allowed(spec_policy: Policy) -> None:
    assert is_skill_profile_allowed(spec_policy, "security-review")
    enforce_skill_profile_allowed(spec_policy, "security-review")  # no raise


def test_requested_profile_outside_whitelist_raises(spec_policy: Policy) -> None:
    assert not is_skill_profile_allowed(spec_policy, "yolo-ship-it")
    with pytest.raises(SkillProfileNotAllowedError) as exc:
        enforce_skill_profile_allowed(spec_policy, "yolo-ship-it")
    assert exc.value.profile == "yolo-ship-it"
    assert "security-review" in exc.value.allowed


def test_none_request_falls_back_to_default(spec_policy: Policy) -> None:
    assert is_skill_profile_allowed(spec_policy, None)
    enforce_skill_profile_allowed(spec_policy, None)  # no raise


def test_default_profile_is_always_allowed() -> None:
    policy = _policy(default="backend-tdd", allowed=["other"])
    assert is_skill_profile_allowed(policy, "backend-tdd")
    enforce_skill_profile_allowed(policy, "backend-tdd")


def test_empty_whitelist_is_unconstrained() -> None:
    policy = _policy()  # no default, no allowed -> no restriction declared
    assert allowed_skill_profiles(policy) == frozenset()
    assert is_skill_profile_allowed(policy, "anything")
    enforce_skill_profile_allowed(policy, "anything")  # no raise
