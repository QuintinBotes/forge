"""Policy gates used by the runtime to allow/deny tool calls before dispatch.

A gate maps a :class:`~forge_contracts.ToolCall` (+ the running objective) to a
:class:`~forge_contracts.Decision`. Two implementations are provided:

* :class:`ActionPolicyGate` — self-contained, decides from the objective's
  ``allowed_actions`` / ``restricted_actions`` (the spec Task Schema fields).
* :class:`PolicyEvaluatorGate` — adapts a frozen
  :class:`~forge_contracts.PolicyEvaluator` (``packages/policy-sdk``, Task 1.10)
  so repo ``.forge/policy.yaml`` rules drive decisions once that package lands.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge_contracts import (
    AgentObjective,
    Decision,
    DecisionEffect,
    Policy,
    PolicyEvaluator,
    ToolCall,
)

__all__ = ["ActionPolicyGate", "PolicyEvaluatorGate", "PolicyGate"]


@runtime_checkable
class PolicyGate(Protocol):
    """Decide whether a tool call may proceed."""

    def evaluate(self, call: ToolCall, objective: AgentObjective) -> Decision: ...


class ActionPolicyGate:
    """Decide from the objective's allowed/restricted action lists.

    Rules (most-restrictive first):

    1. An action in ``restricted_actions`` is always denied.
    2. If ``allowed_actions`` is non-empty, an action not in it is denied
       (allow-list semantics).
    3. Otherwise the action is allowed.
    """

    def evaluate(self, call: ToolCall, objective: AgentObjective) -> Decision:
        action = call.action or call.tool
        if action in objective.restricted_actions:
            return Decision(
                effect=DecisionEffect.DENY,
                reason=f"action '{action}' is restricted",
                matched_rule="restricted_actions",
            )
        if objective.allowed_actions and action not in objective.allowed_actions:
            return Decision(
                effect=DecisionEffect.DENY,
                reason=f"action '{action}' is not in allowed_actions",
                matched_rule="allowed_actions",
            )
        return Decision(
            effect=DecisionEffect.ALLOW,
            reason=f"action '{action}' permitted",
            matched_rule="default_allow",
        )


class PolicyEvaluatorGate:
    """Adapt a frozen :class:`PolicyEvaluator` + :class:`Policy` to a gate."""

    def __init__(self, evaluator: PolicyEvaluator, policy: Policy) -> None:
        self._evaluator = evaluator
        self._policy = policy

    def evaluate(self, call: ToolCall, objective: AgentObjective) -> Decision:
        # ``objective`` is part of the gate interface; policy comes from the file.
        del objective
        return self._evaluator.evaluate(call, self._policy)
