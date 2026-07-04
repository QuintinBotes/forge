"""F22 — multi-repo policy loading + repo-scoped evaluation.

Covers AC 2 (per-repo policy enforcement: same path allowed in one repo, denied
in another) and AC 4 (an unknown/out-of-scope repo is denied, not coerced).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from forge_contracts import DecisionEffect, ToolCall, UnknownRepoError
from forge_policy import (
    PolicyLoadError,
    RepoScopedPolicyEvaluator,
    load_policies,
)

API_POLICY = textwrap.dedent(
    """
    repo_id: github.com/org/api
    write_rules:
      allow: [app/**, tests/**, shared/**]
      deny: [infra/prod/**]
    commands: {test: pytest -q}
    allowed_actions: [read_repo, write_code, run_tests]
    """
).strip()

WEB_POLICY = textwrap.dedent(
    """
    repo_id: github.com/org/web
    write_rules:
      allow: [src/**, tests/**]
      deny: [shared/**, infra/prod/**]
    commands: {test: pnpm test}
    allowed_actions: [read_repo, write_code, run_tests]
    """
).strip()


def _write_policy(root: Path, body: str) -> Path:
    forge = root / ".forge"
    forge.mkdir(parents=True)
    (forge / "policy.yaml").write_text(body, encoding="utf-8")
    return root


@pytest.fixture
def two_repo_roots(tmp_path: Path) -> dict[str, str]:
    api = _write_policy(tmp_path / "api", API_POLICY)
    web = _write_policy(tmp_path / "web", WEB_POLICY)
    return {"github.com/org/api": str(api), "github.com/org/web": str(web)}


def test_load_policies_returns_one_per_repo(two_repo_roots: dict[str, str]) -> None:
    policies = load_policies(two_repo_roots)
    assert set(policies) == {"github.com/org/api", "github.com/org/web"}
    assert policies["github.com/org/api"].repo_id == "github.com/org/api"
    assert policies["github.com/org/web"].repo_id == "github.com/org/web"


def test_load_policies_fail_closed_names_the_repo(tmp_path: Path) -> None:
    good = _write_policy(tmp_path / "api", API_POLICY)
    missing = tmp_path / "web"  # no .forge/policy.yaml
    missing.mkdir()
    with pytest.raises(PolicyLoadError) as exc:
        load_policies({"github.com/org/api": str(good), "github.com/org/web": str(missing)})
    assert exc.value.repo == "github.com/org/web"


def test_guard_selects_policy_by_repo(two_repo_roots: dict[str, str]) -> None:
    """AC 2: shared/x.py is allowed in api but denied in web — same path."""
    evaluator = RepoScopedPolicyEvaluator(load_policies(two_repo_roots))

    allowed = evaluator.evaluate(
        ToolCall(tool="write_code", arguments={"repo": "github.com/org/api", "path": "shared/x.py"})
    )
    assert allowed.effect is DecisionEffect.ALLOW

    denied = evaluator.evaluate(
        ToolCall(tool="write_code", arguments={"repo": "github.com/org/web", "path": "shared/x.py"})
    )
    assert denied.effect is DecisionEffect.DENY


def test_unknown_repo_raises(two_repo_roots: dict[str, str]) -> None:
    """AC 4: a repo not in scope raises UnknownRepoError (no implicit default)."""
    evaluator = RepoScopedPolicyEvaluator(load_policies(two_repo_roots))
    with pytest.raises(UnknownRepoError):
        evaluator.evaluate(
            ToolCall(tool="read_repo", arguments={"repo": "github.com/org/other", "path": "a"})
        )


def test_missing_repo_field_raises(two_repo_roots: dict[str, str]) -> None:
    evaluator = RepoScopedPolicyEvaluator(load_policies(two_repo_roots))
    with pytest.raises(UnknownRepoError):
        evaluator.evaluate(ToolCall(tool="read_repo", arguments={"path": "a"}))


def test_repo_in_metadata_fallback(two_repo_roots: dict[str, str]) -> None:
    evaluator = RepoScopedPolicyEvaluator(load_policies(two_repo_roots))
    decision = evaluator.evaluate(
        ToolCall(
            tool="write_code",
            arguments={"path": "app/x.py"},
            metadata={"repo": "github.com/org/api"},
        )
    )
    assert decision.effect is DecisionEffect.ALLOW


def test_policy_for_unknown_repo_raises(two_repo_roots: dict[str, str]) -> None:
    evaluator = RepoScopedPolicyEvaluator(load_policies(two_repo_roots))
    with pytest.raises(UnknownRepoError):
        evaluator.policy_for("github.com/org/nope")
