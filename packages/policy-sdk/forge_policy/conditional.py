"""The conditional policy evaluator (F29) — extends, never replaces, F04.

:class:`ConditionalPolicyEvaluator` wraps the flat F04 :class:`RepoPolicyEvaluator`
as its base layer and composes the matched conditional ``rules`` on top under a
documented, fail-closed precedence ladder.

The headline guarantee (the security-critical part): the conditional layer can
*tighten* freely (``deny`` / ``require_approval``) but can only *loosen*
(``allow``) a **non-critical** base denial, and only with an explicit
``override_base: true`` rule — it can **never** loosen a critical base denial
(path traversal, secret files, disabled/restricted deploy) **nor the
human-approval-before-merge gate**. A ``schema_version: 1`` policy (no rules)
evaluates byte-for-byte identically to F04 (regression-locked, F29 AC1).
"""

from __future__ import annotations

from pathlib import Path

from forge_contracts import (
    POLICY_CONDITION_FIELDS,
    ApprovalGate,
    ConditionalMatch,
    Decision,
    DecisionEffect,
    Policy,
    RuleEffect,
    ToolCall,
    evaluate_condition,
)
from forge_contracts.dtos import ConditionalRule
from forge_policy.context import PolicyContext
from forge_policy.evaluator import MERGE_ACTIONS, RepoPolicyEvaluator


def _effective_action(call: ToolCall) -> str:
    return (call.action or call.tool or "").strip()


class ConditionalPolicyEvaluator:
    """A :class:`~forge_contracts.PolicyEvaluator` with the F29 conditional layer."""

    def __init__(self, base: RepoPolicyEvaluator | None = None) -> None:
        self._base = base or RepoPolicyEvaluator()

    def load(self, repo_root: str | Path) -> Policy:
        return self._base.load(repo_root)

    def evaluate(self, action: ToolCall, policy: Policy) -> Decision:
        """Backward-compatible 2-arg call (F04 shape) == empty context."""
        return self.evaluate_in_context(action, policy, PolicyContext.empty())

    def evaluate_in_context(
        self, action: ToolCall, policy: Policy, context: PolicyContext
    ) -> Decision:
        """Compose the flat F04 decision with the matched conditional rules."""
        base = self._base.evaluate(action, policy)
        if not policy.rules:
            return base  # regression-locked: pure F04 decision (AC1)

        name = _effective_action(action)
        fields = context.to_fields(action)
        matched = [
            rule
            for rule in policy.rules
            if rule.enabled
            and ("*" in rule.applies_to or name in rule.applies_to)
            and evaluate_condition(rule.when, fields, field_whitelist=POLICY_CONDITION_FIELDS)
        ]
        if not matched:
            return base  # no conditional rule contributed -> verbatim F04 decision

        matched.sort(key=lambda r: (r.priority, policy.rules.index(r)))
        cm = [
            ConditionalMatch(rule_id=r.id, effect=r.effect, severity=r.severity, reason=r.reason)
            for r in matched
        ]

        denies = [r for r in matched if r.effect is RuleEffect.DENY]
        gates = [r for r in matched if r.effect is RuleEffect.REQUIRE_APPROVAL]
        allows = [r for r in matched if r.effect is RuleEffect.ALLOW]

        # (1) Conditional DENY always tightens — wins over everything below the floor.
        if denies:
            r = denies[0]
            return Decision(
                effect=DecisionEffect.DENY,
                reason=r.reason,
                matched_rule=f"rules[{r.id}]",
                severity=r.severity,
                conditional_matches=cm,
                base_effect=base.effect,
            )

        # Loosening is forbidden on the immutable floor: any CRITICAL base decision
        # (path traversal, secret files, disabled/restricted deploy) and the
        # merge/push human-approval gate. Tightening (above / below) is unaffected.
        loosening_forbidden = base.severity == "critical" or name in MERGE_ACTIONS
        override: ConditionalRule | None = None
        if not loosening_forbidden:
            override = next((r for r in allows if r.override_base), None)

        if base.effect is DecisionEffect.DENY:
            if override is not None:
                if gates:
                    return self._gate(gates[0], cm, base.effect)
                return Decision(
                    effect=DecisionEffect.ALLOW,
                    reason=override.reason,
                    matched_rule=f"rules[{override.id}]",
                    severity=override.severity,
                    conditional_matches=cm,
                    base_effect=base.effect,
                )
            # No override (or forbidden): base deny stands — a gate can only annotate.
            return base.model_copy(update={"conditional_matches": cm, "base_effect": base.effect})

        if base.effect is DecisionEffect.REQUIRES_APPROVAL:
            if override is not None and not gates:
                return Decision(
                    effect=DecisionEffect.ALLOW,
                    reason=override.reason,
                    matched_rule=f"rules[{override.id}]",
                    severity=override.severity,
                    conditional_matches=cm,
                    base_effect=base.effect,
                )
            # Gate(s) or no override: the base approval requirement stands.
            return base.model_copy(update={"conditional_matches": cm, "base_effect": base.effect})

        # (4) Base ALLOW: a conditional gate escalates it to approval (tightening).
        if gates:
            return self._gate(gates[0], cm, base.effect)
        return base.model_copy(update={"conditional_matches": cm, "base_effect": base.effect})

    @staticmethod
    def _gate(
        rule: ConditionalRule, cm: list[ConditionalMatch], base_effect: DecisionEffect
    ) -> Decision:
        return Decision(
            effect=DecisionEffect.REQUIRES_APPROVAL,
            reason=rule.reason,
            matched_rule=f"rules[{rule.id}]",
            requires_approval=True,
            approval_gate=ApprovalGate.POLICY_OVERRIDE,
            severity=rule.severity,
            conditional_matches=cm,
            base_effect=base_effect,
        )


__all__ = ["ConditionalPolicyEvaluator"]
