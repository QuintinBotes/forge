"""Tests for the ``.forge/policy.yaml`` loader (plan Task 1.10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_contracts import Policy, PolicyEvaluator
from forge_policy import RepoPolicyEvaluator, load_policy


def test_load_policy_from_repo_root(repo_root: Path) -> None:
    policy = load_policy(repo_root)

    assert isinstance(policy, Policy)
    assert policy.repo_id == "github.com/org/api"
    assert policy.name == "Core API Service"
    assert "app/**" in policy.write_rules.allow
    assert "secrets/**" in policy.write_rules.deny
    assert policy.deploy_rules.allow_agent_deploy is False
    assert "production" in policy.deploy_rules.restricted_environments
    assert "deploy_prod" in policy.restricted_actions
    assert "read_repo" in policy.allowed_actions
    assert policy.commands["test"] == "pytest -q"


def test_load_policy_accepts_string_path(repo_root: Path) -> None:
    policy = load_policy(str(repo_root))
    assert policy.repo_id == "github.com/org/api"


def test_load_policy_accepts_direct_file_path(repo_root: Path) -> None:
    policy = load_policy(repo_root / ".forge" / "policy.yaml")
    assert policy.repo_id == "github.com/org/api"


def test_load_policy_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path)


def test_load_policy_empty_file_raises(tmp_path: Path) -> None:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "policy.yaml").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(tmp_path)


def test_evaluator_load_method(repo_root: Path) -> None:
    evaluator = RepoPolicyEvaluator()
    policy = evaluator.load(repo_root)
    assert policy.repo_id == "github.com/org/api"


def test_evaluator_conforms_to_protocol() -> None:
    assert isinstance(RepoPolicyEvaluator(), PolicyEvaluator)
