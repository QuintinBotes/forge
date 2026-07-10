"""F40-POL-GOVERNANCE — ``PolicyProfile -> .forge/policy.yaml`` bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_policy import (
    POLICY_PROFILES,
    PolicyBootstrapError,
    bootstrap_policy_file,
    load_policy,
    policy_profile,
)


def test_bootstrap_writes_loadable_policy(tmp_path: Path) -> None:
    written = bootstrap_policy_file(tmp_path, profile="default", repo_id="demo/repo")
    assert written == tmp_path / ".forge" / "policy.yaml"
    assert written.is_file()
    # Round-trips: the written file re-loads into an equal policy.
    reloaded = load_policy(tmp_path)
    assert reloaded.repo_id == "demo/repo"
    assert reloaded.review_rules.approval_required_for_merge is True


def test_locked_profile_requires_two_approvals(tmp_path: Path) -> None:
    bootstrap_policy_file(tmp_path, profile="locked", repo_id="secure")
    reloaded = load_policy(tmp_path)
    assert reloaded.review_rules.min_approvals == 2
    assert reloaded.deploy_rules.allow_agent_deploy is False


def test_bootstrap_refuses_to_clobber_without_overwrite(tmp_path: Path) -> None:
    bootstrap_policy_file(tmp_path, profile="default", repo_id="demo")
    with pytest.raises(PolicyBootstrapError):
        bootstrap_policy_file(tmp_path, profile="default", repo_id="demo")
    # Overwrite is explicit and succeeds.
    bootstrap_policy_file(tmp_path, profile="dev", repo_id="demo", overwrite=True)
    assert load_policy(tmp_path).deploy_rules.allow_agent_deploy is True


def test_unknown_profile_raises() -> None:
    with pytest.raises(PolicyBootstrapError):
        policy_profile("nope", "demo")


def test_every_profile_builds_and_serializes(tmp_path: Path) -> None:
    for name in POLICY_PROFILES:
        target = tmp_path / name
        bootstrap_policy_file(target, profile=name, repo_id=f"repo-{name}")
        assert load_policy(target).repo_id == f"repo-{name}"
