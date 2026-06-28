"""Pure routing predicates over the supervision state (F27 §3.3).

Every router is a pure ``SupervisionState -> str`` function — no LLM, no I/O. They
decide the next super-step purely from typed state.
"""

from __future__ import annotations

from forge_coordinator.state import SupervisionState

__all__ = ["router_after_dispatch", "router_after_gate", "router_after_merge"]


def router_after_gate(state: SupervisionState) -> str:
    """After the policy gate: dispatch if permitted with work to do, else finalize."""
    if state.policy_conflict or state.needs_human:
        return "finalize"
    if state.ready_assignments():
        return "dispatch"
    return "finalize"


def router_after_dispatch(state: SupervisionState) -> str:
    """After a dispatch wave: interrupt, loop for more work, or proceed to merge."""
    if state.needs_human:
        return "finalize"
    if state.ready_assignments():
        return "dispatch"
    return "merge"


def router_after_merge(state: SupervisionState) -> str:
    """After merge: a conflict (or any pending interrupt) routes to finalize."""
    if state.merge_result is not None and state.merge_result.conflicts:
        return "finalize"
    if state.needs_human:
        return "finalize"
    return "validate"
