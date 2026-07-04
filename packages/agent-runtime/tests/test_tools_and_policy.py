"""Unit tests for the tool registry and the policy gate."""

from __future__ import annotations

from pathlib import Path

from forge_agent.policy_gate import ActionPolicyGate, PolicyEvaluatorGate
from forge_agent.tools import Tool, ToolRegistry, ToolResult, default_tool_registry
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


def test_tool_policy_action_defaults_to_name() -> None:
    tool = Tool(name="echo", handler=lambda _a: ToolResult(ok=True))
    assert tool.policy_action == "echo"
    # An explicit action overrides the name.
    explicit = Tool(name="w", handler=lambda _a: ToolResult(ok=True), action="write_code")
    assert explicit.policy_action == "write_code"


def test_registry_get_has_names_and_action_for() -> None:
    registry = ToolRegistry()
    registry.add("reader", lambda _a: ToolResult(ok=True), action="read_repo")
    assert registry.get("reader") is not None
    assert registry.get("missing") is None
    assert registry.has("reader") is True
    assert registry.has("missing") is False
    assert registry.names() == ["reader"]
    assert registry.action_for("reader") == "read_repo"
    # An unknown tool's action falls back to the requested name.
    assert registry.action_for("ghost") == "ghost"


def test_default_tool_registry_without_root_is_empty() -> None:
    assert default_tool_registry().names() == []


def test_default_tool_registry_read_write_list(tmp_path: Path) -> None:
    registry = default_tool_registry(tmp_path)
    assert set(registry.names()) == {"read_file", "write_file", "list_dir"}

    write = registry.dispatch("write_file", {"path": "sub/a.txt", "content": "hello"})
    assert write.ok is True
    assert (tmp_path / "sub" / "a.txt").read_text() == "hello"

    read = registry.dispatch("read_file", {"path": "sub/a.txt"})
    assert read.ok is True
    assert read.output == "hello"

    listing = registry.dispatch("list_dir", {"path": "sub"})
    assert listing.ok is True
    assert "a.txt" in listing.output
    assert listing.data["entries"] == ["a.txt"]


def test_default_tool_registry_read_missing_and_list_non_dir(tmp_path: Path) -> None:
    registry = default_tool_registry(tmp_path)
    missing = registry.dispatch("read_file", {"path": "nope.txt"})
    assert missing.ok is False
    assert "not a file" in (missing.error or "")

    (tmp_path / "afile.txt").write_text("x")
    not_dir = registry.dispatch("list_dir", {"path": "afile.txt"})
    assert not_dir.ok is False
    assert "not a directory" in (not_dir.error or "")


def test_default_tool_registry_rejects_path_escape(tmp_path: Path) -> None:
    registry = default_tool_registry(tmp_path)
    # A traversal outside the sandbox root is rejected; dispatch turns the raised
    # ValueError into a structured failure result rather than crashing.
    result = registry.dispatch("read_file", {"path": "../../etc/passwd"})
    assert result.ok is False
    assert "escapes sandbox" in (result.error or "")


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
