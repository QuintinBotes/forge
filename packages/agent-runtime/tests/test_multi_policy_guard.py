"""F22 — repo-aware policy guard (AC 2 per-repo policy, AC 3 path confinement,
AC 4 unknown repo)."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.policy_guard import MultiRepoPolicyGuard
from forge_contracts import (
    DecisionEffect,
    Policy,
    ToolCall,
    UnknownRepoError,
    WriteRules,
)
from forge_policy import RepoPolicyEvaluator


def _policies() -> dict[str, Policy]:
    return {
        "github.com/org/api": Policy(
            repo_id="github.com/org/api",
            write_rules=WriteRules(allow=["app/**", "shared/**"], deny=["infra/prod/**"]),
            allowed_actions=["read_repo", "write_code", "run_tests"],
        ),
        "github.com/org/web": Policy(
            repo_id="github.com/org/web",
            write_rules=WriteRules(allow=["src/**"], deny=["shared/**", "infra/prod/**"]),
            allowed_actions=["read_repo", "write_code", "run_tests"],
        ),
    }


def test_guard_selects_policy_by_repo() -> None:
    """AC 2: shared/x.py allowed in api, denied in web (no merged super-policy)."""
    guard = MultiRepoPolicyGuard(_policies(), RepoPolicyEvaluator())
    allowed = guard.check(
        ToolCall(tool="write_code", arguments={"repo": "github.com/org/api", "path": "shared/x.py"})
    )
    assert allowed.effect is DecisionEffect.ALLOW
    denied = guard.check(
        ToolCall(tool="write_code", arguments={"repo": "github.com/org/web", "path": "shared/x.py"})
    )
    assert denied.effect is DecisionEffect.DENY


def test_unknown_repo_raises() -> None:
    """AC 4: a repo not in scope raises UnknownRepoError."""
    guard = MultiRepoPolicyGuard(_policies(), RepoPolicyEvaluator())
    with pytest.raises(UnknownRepoError):
        guard.check(
            ToolCall(tool="read_repo", arguments={"repo": "github.com/org/other", "path": "a"})
        )


def test_missing_repo_raises() -> None:
    guard = MultiRepoPolicyGuard(_policies(), RepoPolicyEvaluator())
    with pytest.raises(UnknownRepoError):
        guard.check(ToolCall(tool="read_repo", arguments={"path": "a"}))


def test_cross_repo_path_escape_denied(tmp_path: Path) -> None:
    """AC 3: repo=api but path resolving into web's worktree is denied."""
    api_wt = tmp_path / "api"
    web_wt = tmp_path / "web"
    api_wt.mkdir()
    web_wt.mkdir()
    guard = MultiRepoPolicyGuard(
        _policies(),
        RepoPolicyEvaluator(),
        worktrees={"github.com/org/api": api_wt, "github.com/org/web": web_wt},
    )
    # Relative escape into the sibling web worktree.
    decision = guard.check(
        ToolCall(
            tool="write_code",
            arguments={"repo": "github.com/org/api", "path": "../web/secret.py"},
        )
    )
    assert decision.effect is DecisionEffect.DENY
    assert decision.matched_rule == "worktree_confinement"


def test_absolute_path_escape_denied(tmp_path: Path) -> None:
    api_wt = tmp_path / "api"
    api_wt.mkdir()
    guard = MultiRepoPolicyGuard(
        _policies(),
        RepoPolicyEvaluator(),
        worktrees={"github.com/org/api": api_wt},
    )
    decision = guard.check_write_path("github.com/org/api", "/etc/passwd")
    assert decision.effect is DecisionEffect.DENY


def test_confined_path_allowed(tmp_path: Path) -> None:
    api_wt = tmp_path / "api"
    api_wt.mkdir()
    guard = MultiRepoPolicyGuard(
        _policies(),
        RepoPolicyEvaluator(),
        worktrees={"github.com/org/api": api_wt},
    )
    decision = guard.check_write_path("github.com/org/api", "app/main.py")
    assert decision.effect is DecisionEffect.ALLOW


def test_check_write_path_unknown_repo_raises() -> None:
    guard = MultiRepoPolicyGuard(_policies(), RepoPolicyEvaluator(), worktrees={})
    with pytest.raises(UnknownRepoError):
        guard.check_write_path("github.com/org/api", "app/x.py")


def test_check_command_scoped_to_repo() -> None:
    guard = MultiRepoPolicyGuard(_policies(), RepoPolicyEvaluator())
    decision = guard.check_command("github.com/org/api", "run_tests")
    assert decision.effect is DecisionEffect.ALLOW
    with pytest.raises(UnknownRepoError):
        guard.check_command("github.com/org/nope", "run_tests")
