"""F29 — conditional policy evaluation, simulation, testing, and audit.

The service composes the pure ``forge_policy.ConditionalPolicyEvaluator`` with
the persistence + audit side effects the API needs:

* :meth:`simulate` — a pure dry-run with a per-rule trace (no persistence).
* :meth:`evaluate_and_record` — evaluate with a :class:`PolicyContext` and, when
  ≥1 conditional rule contributed and a run/step id is supplied, write exactly
  one append-only ``policy_rule_evaluation`` row **and** emit a compact
  ``policy.decision`` event through the injected :class:`PolicyAuditSink`.
* :meth:`run_tests` — run a ``.forge/policy.tests.yaml`` assertion suite.
* :meth:`list_rule_evaluations` — workspace-scoped audit query (newest first).

Foundation deviation (see slice notes): the central F39 ``AuditSink`` /
``audit_log`` does not exist in this foundation, so F29 defines a minimal
:class:`PolicyAuditSink` seam (constructor-injected, in-memory by default) that a
future F39 ``SqlAuditWriter`` can satisfy. The durable, queryable audit is the
append-only ``policy_rule_evaluation`` row (DB-level immutability trigger).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forge_api.schemas.policy import RuleTrace, SimulationResult
from forge_contracts import Decision, Policy, ToolCall, evaluate_condition
from forge_db.models import PolicyRuleEvaluation
from forge_policy import POLICY_CONDITION_FIELDS, ConditionalPolicyEvaluator, PolicyContext
from forge_policy.tests_runner import PolicyTestReport, PolicyTestSuite, run_policy_tests


class PolicyDecisionEvent(BaseModel):
    """The compact, redacted ``policy.decision`` audit event (F29).

    Carries only the redacted projection — never raw ``ToolCall.args`` or
    ``command`` — mirroring F04/F10 trace redaction.
    """

    action: str
    base_effect: str
    final_effect: str
    requires_approval: bool
    severity: str
    matched_rule_ids: list[str] = Field(default_factory=list)
    context_redacted: dict[str, Any] = Field(default_factory=dict)
    workspace_id: uuid.UUID
    agent_run_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None


@runtime_checkable
class PolicyAuditSink(Protocol):
    """The audit seam F29 emits ``policy.decision`` events through."""

    def emit(self, event: PolicyDecisionEvent) -> None: ...


class InMemoryPolicyAuditSink:
    """A test/seam double recording emitted events (the default sink)."""

    def __init__(self) -> None:
        self.events: list[PolicyDecisionEvent] = []

    def emit(self, event: PolicyDecisionEvent) -> None:
        self.events.append(event)


class PolicyService:
    """Conditional policy evaluation + audit (constructor-injectable seams)."""

    def __init__(
        self,
        evaluator: ConditionalPolicyEvaluator | None = None,
        audit_sink: PolicyAuditSink | None = None,
    ) -> None:
        self._evaluator = evaluator or ConditionalPolicyEvaluator()
        self._audit_sink = audit_sink or InMemoryPolicyAuditSink()

    @property
    def audit_sink(self) -> PolicyAuditSink:
        return self._audit_sink

    # ----------------------------------------------------------------- simulate
    def simulate(
        self, action: ToolCall, policy: Policy, context: PolicyContext
    ) -> SimulationResult:
        """Pure dry-run: the composed :class:`Decision` + a per-rule trace."""
        decision = self._evaluator.evaluate_in_context(action, policy, context)
        name = (action.action or action.tool or "").strip()
        fields = context.to_fields(action)
        traces: list[RuleTrace] = []
        for rule in policy.rules:
            applies = "*" in rule.applies_to or name in rule.applies_to
            matched = bool(
                rule.enabled
                and applies
                and evaluate_condition(rule.when, fields, field_whitelist=POLICY_CONDITION_FIELDS)
            )
            traces.append(
                RuleTrace(rule_id=rule.id, matched=matched, effect=rule.effect, reason=rule.reason)
            )
        # DecisionEffect (StrEnum) values are exactly the SimulationResult literal;
        # annotate to stop the local widening to ``str``.
        base_effect: Literal["allow", "deny", "requires_approval"] = (
            decision.base_effect or decision.effect
        ).value
        return SimulationResult(decision=decision, base_effect=base_effect, traces=traces)

    # --------------------------------------------------------- evaluate + record
    def evaluate_and_record(
        self,
        session: Session,
        *,
        workspace_id: uuid.UUID,
        action: ToolCall,
        policy: Policy,
        context: PolicyContext,
        agent_run_id: uuid.UUID | None = None,
        step_id: uuid.UUID | None = None,
        policy_snapshot_id: uuid.UUID | None = None,
    ) -> Decision:
        """Evaluate in context and persist + audit when a conditional rule fired.

        Writes exactly one append-only ``policy_rule_evaluation`` row **iff** ≥1
        conditional rule contributed, and emits one ``policy.decision`` event.
        """
        decision = self._evaluator.evaluate_in_context(action, policy, context)
        if not decision.conditional_matches:
            return decision  # pure flat decision — nothing to record (F29 §3.1)

        matched_rule_ids = [m.rule_id for m in decision.conditional_matches]
        context_redacted = context.to_redacted_fields()
        base_effect = (decision.base_effect or decision.effect).value

        row = PolicyRuleEvaluation(
            workspace_id=workspace_id,
            agent_run_id=agent_run_id,
            step_id=step_id,
            policy_snapshot_id=policy_snapshot_id,
            action=(action.action or action.tool or "").strip(),
            base_effect=base_effect,
            final_effect=decision.effect.value,
            requires_approval=decision.requires_approval,
            severity=decision.severity,
            matched_rule_ids=matched_rule_ids,
            context_redacted=context_redacted,
        )
        session.add(row)
        session.flush()

        self._audit_sink.emit(
            PolicyDecisionEvent(
                action=row.action,
                base_effect=base_effect,
                final_effect=decision.effect.value,
                requires_approval=decision.requires_approval,
                severity=decision.severity,
                matched_rule_ids=matched_rule_ids,
                context_redacted=context_redacted,
                workspace_id=workspace_id,
                agent_run_id=agent_run_id,
                step_id=step_id,
            )
        )
        return decision

    # --------------------------------------------------------------- run tests
    def run_tests(self, policy: Policy, suite: PolicyTestSuite) -> PolicyTestReport:
        return run_policy_tests(policy, suite)

    # ----------------------------------------------------------- audit queries
    def list_rule_evaluations(
        self,
        session: Session,
        *,
        workspace_id: uuid.UUID,
        agent_run_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[PolicyRuleEvaluation]:
        """Workspace-scoped audit rows, newest first."""
        stmt = select(PolicyRuleEvaluation).where(PolicyRuleEvaluation.workspace_id == workspace_id)
        if agent_run_id is not None:
            stmt = stmt.where(PolicyRuleEvaluation.agent_run_id == agent_run_id)
        stmt = stmt.order_by(PolicyRuleEvaluation.evaluated_at.desc()).limit(limit)
        return list(session.execute(stmt).scalars().all())


__all__ = [
    "InMemoryPolicyAuditSink",
    "PolicyAuditSink",
    "PolicyDecisionEvent",
    "PolicyService",
]
