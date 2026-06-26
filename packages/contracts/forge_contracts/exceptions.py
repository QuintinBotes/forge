"""Typed exceptions raised across the frozen contract surface.

Phase 1 packages raise these so callers can catch a stable, shared type rather
than implementation-specific errors.
"""

from __future__ import annotations


class ForgeError(Exception):
    """Base class for all Forge contract-level errors."""


class CycleError(ForgeError):
    """Raised when adding a task dependency would create a cycle.

    Spec / plan: ``BoardService.dependency_add`` must reject cycles.
    """


class UnknownSkillProfileError(ForgeError, KeyError):
    """Raised when a requested skill profile is not registered.

    Plan Task 1.11: ``SkillProfileRegistry.get`` on an unknown profile raises.
    """


class SpecGateError(ForgeError):
    """Raised when a spec gating rule blocks an action.

    Spec gating: no implementation run without an approved spec; no merge
    without a validation pass.
    """


class PolicyViolationError(ForgeError):
    """Raised when a tool call is denied by repo policy and execution proceeds."""


class ApprovalRequiredError(ForgeError):
    """Raised when an action needs human approval that has not been granted."""


class MCPWriteForbiddenError(ForgeError):
    """Raised when a write tool is invoked on a read-only MCP connection.

    Spec MCP rule 1: connections default to ``allow_write: false``.
    """


__all__ = [
    "ApprovalRequiredError",
    "CycleError",
    "ForgeError",
    "MCPWriteForbiddenError",
    "PolicyViolationError",
    "SpecGateError",
    "UnknownSkillProfileError",
]
