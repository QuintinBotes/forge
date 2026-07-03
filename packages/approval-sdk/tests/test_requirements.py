"""Unit tests — GateRequirementResolver spec defaults (F36 AC#19)."""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_approval.models import GateType
from forge_approval.requirements import GateRequirementResolver
from forge_contracts.dtos import ApprovalPolicy, DeployRules, ReviewRules, SkillProfile


@dataclass
class FakeTask:
    requires_approval: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    kind: str = "feature"


@dataclass
class FakePolicy:
    review_rules: ReviewRules = field(default_factory=ReviewRules)
    deploy_rules: DeployRules = field(default_factory=DeployRules)


def test_pr_required_by_default() -> None:
    assert GateRequirementResolver.is_required(GateType.PR, task=FakeTask(), policy=FakePolicy())


def test_pr_relaxed_only_when_task_and_policy_relax() -> None:
    task = FakeTask(requires_approval=ApprovalPolicy(pr=False))
    strict_policy = FakePolicy()
    assert GateRequirementResolver.is_required(GateType.PR, task=task, policy=strict_policy)
    relaxed = FakePolicy(review_rules=ReviewRules(approval_required_for_merge=False))
    assert not GateRequirementResolver.is_required(GateType.PR, task=task, policy=relaxed)


def test_spec_required_for_feature_class() -> None:
    assert GateRequirementResolver.is_required(GateType.SPEC, task=FakeTask(kind="feature"))
    assert not GateRequirementResolver.is_required(GateType.SPEC, task=FakeTask(kind="chore"))
    explicit = FakeTask(kind="chore", requires_approval=ApprovalPolicy(spec=True))
    assert GateRequirementResolver.is_required(GateType.SPEC, task=explicit)


def test_plan_follows_task_flag() -> None:
    assert not GateRequirementResolver.is_required(GateType.PLAN, task=FakeTask())
    on = FakeTask(requires_approval=ApprovalPolicy(plan=True))
    assert GateRequirementResolver.is_required(GateType.PLAN, task=on)


def test_deploy_required_for_restricted_env() -> None:
    task = FakeTask(requires_approval=ApprovalPolicy(deploy=False))
    policy = FakePolicy(
        deploy_rules=DeployRules(
            allow_agent_deploy=True, restricted_environments=["production"]
        )
    )
    assert GateRequirementResolver.is_required(
        GateType.DEPLOY, task=task, policy=policy, target_environment="production"
    )
    assert not GateRequirementResolver.is_required(
        GateType.DEPLOY, task=task, policy=policy, target_environment="staging"
    )


def test_deploy_required_when_agent_deploy_forbidden() -> None:
    task = FakeTask(requires_approval=ApprovalPolicy(deploy=False))
    policy = FakePolicy(deploy_rules=DeployRules(allow_agent_deploy=False))
    assert GateRequirementResolver.is_required(
        GateType.DEPLOY, task=task, policy=policy, target_environment="staging"
    )


def test_incident_remediation_follows_skill() -> None:
    assert GateRequirementResolver.is_required(GateType.INCIDENT_REMEDIATION, skill=None)
    on = SkillProfile(name="incident-response", requires_human_approval_before_action=True)
    off = SkillProfile(name="docs")
    assert GateRequirementResolver.is_required(GateType.INCIDENT_REMEDIATION, skill=on)
    assert not GateRequirementResolver.is_required(GateType.INCIDENT_REMEDIATION, skill=off)


def test_policy_override_always_required() -> None:
    relaxed_task = FakeTask(
        requires_approval=ApprovalPolicy(spec=False, plan=False, pr=False, deploy=False)
    )
    assert GateRequirementResolver.is_required(GateType.POLICY_OVERRIDE, task=relaxed_task)
