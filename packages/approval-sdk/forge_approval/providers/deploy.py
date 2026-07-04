"""The ``deploy`` gate primitive (J4).

An env-promotion request blocked by ``deploy_rules`` raises this gate. The
gate only AUTHORIZES the promotion — F36 never deploys anything; the actual
promotion executes downstream (F31's env-promotion control plane consumes the
``deploy.approved`` domain signal riding on ``approval.resolved``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from forge_approval.models import (
    ApprovalAction,
    ApprovalContext,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateType,
    Principal,
    ResolutionOutcome,
    RiskFlag,
)
from forge_approval.registry import default_actions

#: Domain signal name carried in the resolution outcome (NOT a bus event type).
DEPLOY_APPROVED_SIGNAL = "deploy.approved"


class DeployGateProvider:
    """Builds the deploy gate's must-show context from its payload snapshot."""

    gate_type: ClassVar[GateType] = GateType.DEPLOY

    async def build_context(
        self, request: ApprovalRequest, *, session: Any = None
    ) -> ApprovalContext:
        payload = dict(request.gate_payload)
        environment = payload.get("environment")
        risk_flags: list[RiskFlag] = [
            RiskFlag(
                severity=flag.get("severity", "warning"),
                category=flag.get("category", "policy"),
                message=flag.get("message", ""),
                source=flag.get("source"),
            )
            for flag in payload.get("risk_flags", [])
            if isinstance(flag, dict)
        ]
        if payload.get("restricted_environment"):
            risk_flags.append(
                RiskFlag(
                    severity="critical",
                    category="restricted_env",
                    message=f"target environment '{environment}' is restricted",
                    source="deploy_rules",
                )
            )
        return ApprovalContext(
            approval_id=request.id,
            gate_type=request.gate_type,
            goal=request.title
            or f"Promote {payload.get('source_commit', 'HEAD')} to {environment}",
            diff=payload.get("diff"),
            verification=payload.get("verification"),
            risk_flags=risk_flags,
            run_trace_ref=_run_trace_ref(request),
            available_actions=self.available_actions(request),
            gate_payload=payload,
        )

    def available_actions(self, request: ApprovalRequest) -> list[ApprovalAction]:
        return default_actions(request.gate_type)


class DeployResolutionHook:
    """Records the authorization outcome; execution stays downstream (AC#15)."""

    gate_type: ClassVar[GateType] = GateType.DEPLOY

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome:
        environment = request.gate_payload.get("environment")
        if decision.decision is ApprovalAction.APPROVE:
            return ResolutionOutcome(
                completed=True,
                follow_up_state="deploy_approved",
                details={
                    "signal": DEPLOY_APPROVED_SIGNAL,
                    "environment": environment,
                    "executed_by": "downstream",  # F36 does not deploy
                },
            )
        if decision.decision is ApprovalAction.REJECT:
            return ResolutionOutcome(
                completed=True,
                follow_up_state="deploy_denied",
                details={"environment": environment},
            )
        return ResolutionOutcome(
            completed=True,
            follow_up_state="needs_human_input",
            details={"environment": environment},
        )


def _run_trace_ref(request: ApprovalRequest) -> dict[str, Any] | None:
    if not (request.workflow_run_id or request.agent_run_id):
        return None
    return {
        "workflow_run_id": str(request.workflow_run_id) if request.workflow_run_id else None,
        "agent_run_id": str(request.agent_run_id) if request.agent_run_id else None,
    }


__all__ = ["DEPLOY_APPROVED_SIGNAL", "DeployGateProvider", "DeployResolutionHook"]
