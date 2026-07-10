"""Write-approval evaluator for MCP tool calls (F40 delta 1).

Wires the (previously inert) :meth:`MCPGatewayClient._policy_gate` approval path
to the F36 human-approval gate. A read-only connection already rejects write
tools outright (spec rule 1). This evaluator covers the *allow_write* case: a
connection may advertise a write tool, but invoking it does not execute
directly — evaluation returns ``requires_approval=True``, the client raises
:class:`~forge_contracts.ApprovalRequiredError`, and the control plane maps that
to ``403`` (fail-closed). Read tools pass straight through (allowed).

It structurally satisfies the frozen
:class:`forge_contracts.protocols.PolicyEvaluator` Protocol, so it drops into
:class:`~forge_mcp.MCPConnectionManager` exactly where a repo policy evaluator
would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from forge_contracts import Decision, DecisionEffect, Policy, ToolCall
from forge_mcp.security import is_write_tool

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["MCPWriteApprovalEvaluator", "default_mcp_policy"]


def default_mcp_policy() -> Policy:
    """A minimal policy so the client's ``policy and evaluator`` guard is armed."""
    return Policy(repo_id="mcp")


class MCPWriteApprovalEvaluator:
    """A ``PolicyEvaluator`` that routes write MCP tool calls through approval."""

    def load(self, repo_root: str | Path) -> Policy:  # pragma: no cover - unused
        return default_mcp_policy()

    def evaluate(self, action: ToolCall, policy: Policy) -> Decision:
        if is_write_tool(action.tool):
            return Decision(
                effect=DecisionEffect.ALLOW,
                requires_approval=True,
                reason=f"write MCP tool {action.tool!r} requires human approval",
            )
        return Decision(effect=DecisionEffect.ALLOW)
