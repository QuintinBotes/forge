"""Gate provider/hook registry — the plug-in surface for gate-owning slices.

Gate owners (F08 pr, F02 spec/plan, F17 incident, F36 deploy/policy_override)
register a :class:`GateContextProvider` (builds the nine "must-show" items) and
optionally a :class:`GateResolutionHook` (the side effect on resolve) at the
composition root. A gate with no provider degrades to a read-only fallback
context; a gate with no hook resolves with a ``not_implemented`` outcome —
never a crash (slice risk #3).
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol, runtime_checkable

from forge_approval.models import (
    ApprovalAction,
    ApprovalContext,
    ApprovalDecisionRequest,
    ApprovalRequest,
    GateType,
    Principal,
    ResolutionOutcome,
)


class MissingProviderError(LookupError):
    """No :class:`GateContextProvider` is registered for a gate type."""

    def __init__(self, gate_type: GateType) -> None:
        self.gate_type = gate_type
        super().__init__(f"no context provider registered for gate '{gate_type.value}'")


@runtime_checkable
class GateContextProvider(Protocol):
    """Builds the gate-type-specific :class:`ApprovalContext` (9 items)."""

    gate_type: ClassVar[GateType]

    async def build_context(
        self, request: ApprovalRequest, *, session: Any = None
    ) -> ApprovalContext: ...

    def available_actions(self, request: ApprovalRequest) -> list[ApprovalAction]: ...


@runtime_checkable
class GateResolutionHook(Protocol):
    """Performs the gate-specific side effect AFTER the decision is recorded."""

    gate_type: ClassVar[GateType]

    async def on_resolved(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecisionRequest,
        actor: Principal,
        *,
        session: Any = None,
    ) -> ResolutionOutcome: ...


class GateRegistry:
    """Register/lookup providers and hooks by gate type."""

    def __init__(self) -> None:
        self._providers: dict[GateType, GateContextProvider] = {}
        self._hooks: dict[GateType, GateResolutionHook] = {}

    def register_provider(self, provider: GateContextProvider) -> None:
        self._providers[provider.gate_type] = provider

    def register_hook(self, hook: GateResolutionHook) -> None:
        self._hooks[hook.gate_type] = hook

    def provider(self, gate_type: GateType) -> GateContextProvider:
        """Return the registered provider; raises :class:`MissingProviderError`."""
        try:
            return self._providers[gate_type]
        except KeyError:
            raise MissingProviderError(gate_type) from None

    def has_provider(self, gate_type: GateType) -> bool:
        return gate_type in self._providers

    def hook(self, gate_type: GateType) -> GateResolutionHook | None:
        """Return the registered hook, or ``None`` for an emit-only gate."""
        return self._hooks.get(gate_type)


#: The default action set; ``escalate`` is additionally offered for the two
#: high-risk gates (spec: Approval UI Must Show item 9).
BASE_ACTIONS: tuple[ApprovalAction, ...] = (
    ApprovalAction.APPROVE,
    ApprovalAction.REJECT,
    ApprovalAction.REQUEST_CHANGES,
)
ESCALATABLE_GATES: frozenset[GateType] = frozenset(
    {GateType.INCIDENT_REMEDIATION, GateType.POLICY_OVERRIDE}
)


def default_actions(gate_type: GateType) -> list[ApprovalAction]:
    """Gate-correct action list (AC#4)."""
    actions = list(BASE_ACTIONS)
    if gate_type in ESCALATABLE_GATES:
        actions.append(ApprovalAction.ESCALATE)
    return actions


__all__ = [
    "BASE_ACTIONS",
    "ESCALATABLE_GATES",
    "GateContextProvider",
    "GateRegistry",
    "GateResolutionHook",
    "MissingProviderError",
    "default_actions",
]
