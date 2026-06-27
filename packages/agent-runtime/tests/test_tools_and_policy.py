"""Unit tests for the tool registry and the policy gate."""

from __future__ import annotations

from forge_agent.policy_gate import ActionPolicyGate, PolicyEvaluatorGate
from forge_agent.tools import Tool, ToolRegistry, ToolResult
from forge_contracts import (
    AgentObjective,
    Decision,
    DecisionEffect,
    Policy,
    ToolCall,
)


def test_registry_dispatch_runs_handler() -> None:
    registry = ToolRegistry()
    registry.add("echo", lambda args: ToolResult(ok=True, output=str(args.get("x"))))
    result = registry.dispatch("echo", {"x": 7})
    assert result.ok is True
    assert result.output == "7"


def test_registry_unknown_tool_is_not_ok() -> None:
    registry = ToolRegistry()
    result = registry.dispatch("nope", {})
    assert result.ok is False
    assert result.error is not None
    assert "nope" in result.error


def test_registry_handler_exception_is_captured() -> None:
    registry = ToolRegistry()

    def boom(_args: dict[str, object]) -> ToolResult:
        raise RuntimeError("kaboom")

    registry.register(Tool(name="boom", handler=boom))
    result = registry.dispatch("boom", {})
    assert result.ok is False
    assert "kaboom" in (result.error or "")


def test_registry_rejects_duplicate() -> None:
    registry = ToolRegistry()
    registry.add("dup", lambda _a: ToolResult(ok=True))
    try:
        registry.add("dup", lambda _a: ToolResult(ok=True))
    except ValueError:
        return
    raise AssertionError("expected ValueError on duplicate tool registration")


def test_registry_schemas_expose_names() -> None:
    registry = ToolRegistry()
    registry.add("read_file", lambda _a: ToolResult(ok=True), description="read a file")
    schemas = registry.schemas()
    assert {"name": "read_file", "description": "read a file"} in schemas


def test_action_gate_denies_restricted_action() -> None:
    gate = ActionPolicyGate()
    obj = AgentObjective(objective="do", restricted_actions=["deploy_prod"])
    call = ToolCall(tool="deploy", action="deploy_prod")
    decision = gate.evaluate(call, obj)
    assert decision.effect is DecisionEffect.DENY
    assert decision.allowed is False


def test_action_gate_denies_action_outside_allowlist() -> None:
    gate = ActionPolicyGate()
    obj = AgentObjective(objective="do", allowed_actions=["read_repo"])
    call = ToolCall(tool="write", action="write_code")
    decision = gate.evaluate(call, obj)
    assert decision.effect is DecisionEffect.DENY


def test_action_gate_allows_listed_action() -> None:
    gate = ActionPolicyGate()
    obj = AgentObjective(objective="do", allowed_actions=["read_repo"])
    call = ToolCall(tool="read_file", action="read_repo")
    decision = gate.evaluate(call, obj)
    assert decision.allowed is True


def test_action_gate_defaults_allow_when_no_allowlist() -> None:
    gate = ActionPolicyGate()
    obj = AgentObjective(objective="do")
    call = ToolCall(tool="anything", action="whatever")
    assert gate.evaluate(call, obj).allowed is True


def test_policy_evaluator_gate_delegates() -> None:
    captured: dict[str, object] = {}

    class _FakeEvaluator:
        def load(self, repo_root: object) -> Policy:  # pragma: no cover - unused
            return Policy(repo_id="x")

        def evaluate(self, action: ToolCall, policy: Policy) -> Decision:
            captured["action"] = action
            captured["policy"] = policy
            return Decision(effect=DecisionEffect.ALLOW, reason="ok")

    policy = Policy(repo_id="repo")
    gate = PolicyEvaluatorGate(_FakeEvaluator(), policy)
    obj = AgentObjective(objective="do")
    call = ToolCall(tool="t", action="a")
    decision = gate.evaluate(call, obj)
    assert decision.allowed is True
    assert captured["policy"] is policy
    assert captured["action"] is call
