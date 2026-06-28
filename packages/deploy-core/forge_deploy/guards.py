"""Deploy FSM guards — pure predicates over the locked deployment row + DB state.

Guards are the deterministic routing logic (never LLM judgement). They double-
enforce the restricted-env approval rule: ``no_approval_required`` can never be
true for a restricted environment, so ``gate_passed -> approved`` cannot fire for
a restricted env even if a buggy caller emits ``gate_passed``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from forge_db.models.deployment import Deployment, Environment
from forge_deploy.repository import DeploymentRepository
from forge_deploy.schemas import GateConfig
from forge_deploy.states import DeploymentEvent, DeploymentKind, GateCheckStatus


@dataclass
class GuardContext:
    repo: DeploymentRepository
    deployment: Deployment
    environment: Environment | None
    event: DeploymentEvent

    def gate_config(self) -> GateConfig:
        cfg = self.environment.gate_config if self.environment else {}
        return GateConfig.model_validate(cfg or {})


GuardFn = Callable[[GuardContext], bool]


def gate_clear(ctx: GuardContext) -> bool:
    """No persisted gate check is ``failed`` for this deployment."""
    return all(
        c.status != GateCheckStatus.FAILED
        for c in ctx.repo.checks(ctx.deployment.id)
    )


def no_approval_required(ctx: GuardContext) -> bool:
    """True only for an unrestricted environment that does not require approval.

    Restricted environments always return False (inviolable approval rule).
    """
    env = ctx.environment
    if env is None:
        return False
    if env.is_restricted:
        return False
    return not env.requires_approval


def approval_granted(ctx: GuardContext) -> bool:
    """``min_approvals`` distinct approvers have approved this deployment."""
    cfg = ctx.gate_config()
    count = ctx.repo.distinct_approver_count(ctx.deployment.id)
    return count >= max(1, cfg.min_approvals)


def auto_rollback_enabled(ctx: GuardContext) -> bool:
    # Never auto-roll-back a rollback deployment (would recurse infinitely).
    if ctx.deployment.kind == DeploymentKind.ROLLBACK:
        return False
    return bool(ctx.gate_config().auto_rollback)


def auto_rollback_disabled(ctx: GuardContext) -> bool:
    return not ctx.gate_config().auto_rollback


def default_guard_registry() -> dict[str, GuardFn]:
    registry: dict[str, GuardFn] = {
        "gate_clear": gate_clear,
        "no_approval_required": no_approval_required,
        "approval_granted": approval_granted,
        "approval_granted:deploy": approval_granted,
        "auto_rollback_enabled": auto_rollback_enabled,
        "auto_rollback_disabled": auto_rollback_disabled,
    }
    return registry


__all__ = [
    "GuardContext",
    "GuardFn",
    "approval_granted",
    "auto_rollback_disabled",
    "auto_rollback_enabled",
    "default_guard_registry",
    "gate_clear",
    "no_approval_required",
]
