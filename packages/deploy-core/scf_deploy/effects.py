"""Deploy FSM effects — the side-effect ports dispatched post-commit.

The engine is side-effect-free except via an injected
:class:`EffectDispatcher`. After a transition is committed, the engine dispatches
the transition's declared effects (names below). The default
:class:`RecordingEffectDispatcher` records them (used by the engine unit tests and
as an audit-only dispatcher); the orchestrator/worker provide a dispatcher that
actually performs them (or simply re-derives them, since the orchestrator drives
the side effects explicitly between transitions).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

#: Every effect name that may appear in the bundled deployment DSL.
KNOWN_EFFECTS: frozenset[str] = frozenset(
    {
        "evaluate_gate",
        "trigger_deploy",
        "create_deploy_approval",
        "notify_approval_requested",
        "notify_gate_failed",
        "notify_rejected",
        "notify_failed",
        "notify_succeeded",
        "notify_rolled_back",
        "run_health_check",
        "record_environment_state",
        "maybe_rollback",
        "start_rollback",
    }
)


@runtime_checkable
class EffectDispatcher(Protocol):
    def dispatch(
        self,
        effect: str,
        *,
        deployment_id: uuid.UUID,
        payload: dict[str, Any],
        actor: str,
    ) -> None: ...


class RecordingEffectDispatcher:
    """Records dispatched effects; performs no side effects (test/audit double)."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, uuid.UUID]] = []

    def dispatch(
        self,
        effect: str,
        *,
        deployment_id: uuid.UUID,
        payload: dict[str, Any],
        actor: str,
    ) -> None:
        self.dispatched.append((effect, deployment_id))


# Backwards-friendly alias.
NullEffectDispatcher = RecordingEffectDispatcher


__all__ = [
    "KNOWN_EFFECTS",
    "EffectDispatcher",
    "NullEffectDispatcher",
    "RecordingEffectDispatcher",
]
