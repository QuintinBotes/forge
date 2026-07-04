"""F22 contract-widening tests: multi-repo DTOs + V1 back-compat properties."""

from __future__ import annotations

import uuid

import forge_contracts as fc


def test_repo_target_v1_defaults_are_backcompat() -> None:
    """A V1-shaped RepoTarget parses with the new fields defaulted."""
    rt = fc.RepoTarget(repo="github.com/org/api", base_branch="main")
    assert rt.role == "secondary"
    assert rt.depends_on == []
    assert rt.skill_profile is None
    assert rt.required_for_merge is True


def test_repo_target_multi_repo_fields() -> None:
    rt = fc.RepoTarget(
        repo="github.com/org/web",
        role="primary",
        depends_on=["github.com/org/api"],
        skill_profile="frontend-ui",
        required_for_merge=False,
    )
    assert rt.role == "primary"
    assert rt.depends_on == ["github.com/org/api"]
    assert rt.skill_profile == "frontend-ui"
    assert rt.required_for_merge is False


def test_objective_primary_repo_target_prefers_role() -> None:
    obj = fc.AgentObjective(
        objective="x",
        repo_targets=[
            fc.RepoTarget(repo="a", role="secondary"),
            fc.RepoTarget(repo="b", role="primary"),
        ],
    )
    assert obj.primary_repo_target is not None
    assert obj.primary_repo_target.repo == "b"
    # V1 back-compat alias.
    assert obj.repo_target is not None
    assert obj.repo_target.repo == "b"


def test_objective_single_repo_target_is_primary_even_if_secondary() -> None:
    """V1 single-repo: the lone target is the primary regardless of role."""
    obj = fc.AgentObjective(
        objective="x",
        repo_targets=[fc.RepoTarget(repo="only", role="secondary")],
    )
    assert obj.primary_repo_target is not None
    assert obj.primary_repo_target.repo == "only"


def test_objective_no_repo_targets() -> None:
    obj = fc.AgentObjective(objective="x")
    assert obj.primary_repo_target is None
    assert obj.repo_target is None


def test_agent_run_result_changed_files_backcompat() -> None:
    result = fc.AgentRunResult(
        repo_change_sets=[
            fc.RepoChangeSet(repo="a", changed_files=["x.py"], has_changes=True),
            fc.RepoChangeSet(repo="b", changed_files=["y.py"], has_changes=True),
        ]
    )
    # V1 callers read .changed_files -> primary (first) repo only.
    assert result.changed_files == ["x.py"]


def test_agent_run_result_changed_files_empty_when_no_change_sets() -> None:
    assert fc.AgentRunResult().changed_files == []


def test_merge_plan_dto() -> None:
    plan = fc.MergePlan(
        primary_repo_id="a",
        merge_order=["a", "b"],
        edges={"a": [], "b": ["a"]},
    )
    assert plan.merge_order == ["a", "b"]


def test_pr_group_and_links() -> None:
    group = fc.PRGroup(
        id=uuid.uuid4(),
        merge_order=["a", "b"],
        prs=[
            fc.CrossPRLink(repo_id="a", pr_number=1, merge_order=0),
            fc.CrossPRLink(repo_id="b", pr_number=2, merge_order=1),
        ],
        status="open",
    )
    assert group.status == "open"
    assert [p.pr_number for p in group.prs] == [1, 2]


def test_merge_gate_result_and_outcome() -> None:
    gate = fc.MultiRepoMergeGateResult(
        can_merge=False,
        repos=[fc.RepoMergeStatus(repo_id="a", blocking_reasons=["a: not approved"])],
        merge_order=["a"],
        blocking_reasons=["a: not approved"],
    )
    outcome = fc.MergeGroupOutcome(status="blocked", gate=gate)
    assert outcome.status == "blocked"
    assert outcome.gate is not None and outcome.gate.can_merge is False


def test_unknown_repo_error_carries_repo() -> None:
    err = fc.UnknownRepoError("github.com/org/x")
    assert err.repo == "github.com/org/x"
    assert isinstance(err, fc.ForgeError)
    assert isinstance(err, KeyError)
