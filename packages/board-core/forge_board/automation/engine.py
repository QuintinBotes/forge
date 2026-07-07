"""The pure automation engine (F21).

``AutomationEngine.evaluate`` selects matching enabled rules (ordered by
``run_order``), applies the loop guard, evaluates trigger config + condition,
plans the action list, and calls ``executor.execute`` for each — returning one
:class:`ExecutionResult` per evaluated rule. It is **pure**: every side effect
goes through the injected :class:`ActionExecutor`, and persistence is the
caller's job (the worker writes one ``automation_execution`` row per result).
"""

from __future__ import annotations

from forge_board.automation.conditions import evaluate_condition
from forge_board.automation.executor import ActionContext, ActionExecutor
from forge_board.automation.loop_guard import LoopGuard
from forge_board.automation.schemas import (
    ActionResult,
    AutomationRuleSpecWithMeta,
    EntitySnapshot,
    ExecutionResult,
)
from forge_board.automation.triggers import trigger_matches
from forge_contracts.automation import (
    AutomationExecutionStatus,
    AutomationTriggerEnvelope,
)


class AutomationEngine:
    """Deterministic predicate evaluation + fixed action dispatch."""

    def __init__(self, loop_guard: LoopGuard | None = None) -> None:
        self._loop_guard = loop_guard or LoopGuard()

    def evaluate(
        self,
        envelope: AutomationTriggerEnvelope,
        rules: list[AutomationRuleSpecWithMeta],
        executor: ActionExecutor,
        snapshot: EntitySnapshot,
    ) -> list[ExecutionResult]:
        """Evaluate every matching rule; return one result per evaluated rule."""
        results: list[ExecutionResult] = []
        for meta in sorted(rules, key=lambda r: (r.run_order, str(r.id))):
            result = self._evaluate_one(envelope, meta, executor, snapshot)
            if result is not None:
                results.append(result)
        return results

    def _evaluate_one(
        self,
        envelope: AutomationTriggerEnvelope,
        meta: AutomationRuleSpecWithMeta,
        executor: ActionExecutor,
        snapshot: EntitySnapshot,
    ) -> ExecutionResult | None:
        spec = meta.spec

        if not meta.enabled:
            return self._result(
                meta, AutomationExecutionStatus.SKIPPED_DISABLED, None, [], [], envelope
            )

        if not trigger_matches(spec.trigger, envelope):
            return None  # rule simply does not apply to this event

        abort = self._loop_guard.abort_reason(envelope, meta.id)
        if abort is not None:
            return self._result(
                meta,
                AutomationExecutionStatus.SKIPPED_LOOP,
                None,
                [],
                [],
                envelope,
                error=abort,
            )

        try:
            condition_ok = evaluate_condition(spec.condition, snapshot)
        except ValueError as exc:
            return self._result(
                meta,
                AutomationExecutionStatus.FAILED,
                None,
                list(spec.actions),
                [],
                envelope,
                error=f"condition_error:{exc}",
            )

        if not condition_ok:
            return self._result(
                meta,
                AutomationExecutionStatus.CONDITIONS_FAILED,
                False,
                list(spec.actions),
                [],
                envelope,
            )

        ctx = ActionContext(
            rule_id=meta.id,
            rule_name=spec.name,
            snapshot=snapshot,
            envelope=envelope,
            depth=envelope.depth,
            causation_chain=list(envelope.causation_chain),
        )

        action_results: list[ActionResult] = []
        for action in spec.actions:
            try:
                action_results.append(executor.execute(action, ctx))
            except Exception as exc:
                action_results.append(
                    ActionResult(type=action.type, status="error", detail={"error": str(exc)})
                )

        status = self._summarize(action_results)
        return self._result(
            meta,
            status,
            True,
            list(spec.actions),
            action_results,
            envelope,
        )

    @staticmethod
    def _summarize(results: list[ActionResult]) -> AutomationExecutionStatus:
        if not results:
            return AutomationExecutionStatus.NO_OP
        statuses = {r.status for r in results}
        if statuses <= {"no_op"}:
            return AutomationExecutionStatus.NO_OP
        has_error = any(s in ("error", "forbidden") for s in statuses)
        has_ok = any(s == "ok" for s in statuses)
        if has_error and has_ok:
            return AutomationExecutionStatus.PARTIAL_FAILURE
        if has_error and not has_ok:
            return AutomationExecutionStatus.FAILED
        return AutomationExecutionStatus.SUCCEEDED

    @staticmethod
    def _result(
        meta: AutomationRuleSpecWithMeta,
        status: AutomationExecutionStatus,
        condition_result: bool | None,
        actions_planned: list[object],
        action_results: list[ActionResult],
        envelope: AutomationTriggerEnvelope,
        *,
        error: str | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            rule_id=meta.id,
            rule_version=meta.version,
            status=status,
            condition_result=condition_result,
            actions_planned=list(actions_planned),
            action_results=action_results,
            depth=envelope.depth,
            causation_chain=list(envelope.causation_chain),
            error=error,
        )


__all__ = ["AutomationEngine"]
