"""``GateRequirementResolver`` — centralizes "is this gate required?" (AC#19).

Reads ``Task.requires_approval{spec,plan,pr,deploy}``, repo ``review_rules`` /
``deploy_rules``, and skill-profile flags. F07's ``plan_not_required`` guard and
the gate producers delegate here so the defaults live in exactly one place.
"""

from __future__ import annotations

from typing import Any

from forge_approval.models import GateType


def _approval_policy(task: Any) -> Any | None:
    return getattr(task, "requires_approval", None) if task is not None else None


class GateRequirementResolver:
    """Spec defaults for whether a gate must be raised."""

    @staticmethod
    def is_required(
        gate_type: GateType,
        *,
        task: Any = None,
        policy: Any = None,
        skill: Any = None,
        target_environment: str | None = None,
    ) -> bool:
        """Return whether ``gate_type`` is required for this task/policy/skill.

        ``task`` carries ``requires_approval`` (an ``ApprovalPolicy``) and
        ``kind``; ``policy`` carries ``review_rules`` / ``deploy_rules``;
        ``skill`` carries ``requires_human_approval_before_action`` /
        ``human_review_required``. All are optional — absent inputs fall back
        to the spec defaults (which always err toward requiring the gate).
        """
        approvals = _approval_policy(task)

        if gate_type is GateType.POLICY_OVERRIDE:
            return True  # always: an out-of-policy call needs a human, period.

        if gate_type is GateType.PR:
            review_rules = getattr(policy, "review_rules", None)
            merge_requires = bool(getattr(review_rules, "approval_required_for_merge", True))
            task_requires = bool(getattr(approvals, "pr", True))
            return task_requires or merge_requires

        if gate_type is GateType.SPEC:
            if approvals is not None and bool(getattr(approvals, "spec", False)):
                return True
            # Feature-class tasks default to spec review even when unset.
            kind = getattr(task, "kind", None)
            kind_value = getattr(kind, "value", kind)
            return task is None or kind_value == "feature"

        if gate_type is GateType.PLAN:
            return bool(getattr(approvals, "plan", False))

        if gate_type is GateType.DEPLOY:
            if bool(getattr(approvals, "deploy", True)):
                return True
            deploy_rules = getattr(policy, "deploy_rules", None)
            restricted = list(getattr(deploy_rules, "restricted_environments", []) or [])
            if target_environment is not None and target_environment in restricted:
                return True
            return not bool(getattr(deploy_rules, "allow_agent_deploy", False))

        if gate_type is GateType.INCIDENT_REMEDIATION:
            if skill is None:
                return True  # incident-response profiles always require a human.
            return bool(
                getattr(skill, "requires_human_approval_before_action", False)
                or getattr(skill, "human_review_required", False)
            )

        return True  # pragma: no cover — exhaustive over GateType


__all__ = ["GateRequirementResolver"]
