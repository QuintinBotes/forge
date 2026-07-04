"""Integration tests for the single-agent plan -> act -> observe runtime."""

from __future__ import annotations

from pathlib import Path

from forge_agent.runtime import AgentRunner
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import (
    AgentObjective,
    AgentRunResult,
    AgentRuntime,
    DecisionEffect,
    HandoffRules,
    RunStatus,
    StepKind,
)


def test_runner_conforms_to_protocol() -> None:
    runner = AgentRunner(model=ScriptedModelClient([]))
    assert isinstance(runner, AgentRuntime)


def test_graph_runs_to_completion_on_scripted_model() -> None:
    registry = ToolRegistry()
    seen: list[dict[str, object]] = []

    def read_file(args: dict[str, object]) -> ToolResult:
        seen.append(args)
        return ToolResult(ok=True, output="file contents")

    registry.add("read_file", read_file, action="read_repo")

    model = ScriptedModelClient(
        [
            tool_response("read_file", {"path": "app/main.py", "action": "read_repo"}),
            finish_response(
                "implemented endpoint",
                confidence=0.95,
                acceptance_criteria_satisfied=["A1"],
            ),
        ]
    )
    runner = AgentRunner(model=model, tools=registry)
    objective = AgentObjective(objective="add endpoint", allowed_actions=["read_repo"])

    result = runner.run(objective)

    assert isinstance(result, AgentRunResult)
    assert result.status is RunStatus.SUCCEEDED
    assert result.output == "implemented endpoint"
    assert result.confidence == 0.95
    assert result.acceptance_criteria_satisfied == ["A1"]
    assert result.needs_human is False
    assert seen == [{"path": "app/main.py", "action": "read_repo"}]
    kinds = {step.kind for step in result.steps}
    assert StepKind.PLAN in kinds
    assert StepKind.TOOL_CALL in kinds
    assert StepKind.OBSERVATION in kinds
    assert StepKind.OUTPUT in kinds


def test_restricted_tool_call_denied_by_policy_gate() -> None:
    registry = ToolRegistry()
    called = {"deploy": False}

    def deploy(_args: dict[str, object]) -> ToolResult:
        called["deploy"] = True
        return ToolResult(ok=True, output="deployed")

    registry.add("deploy", deploy, action="deploy_prod")

    model = ScriptedModelClient(
        [
            tool_response("deploy", {"action": "deploy_prod"}),
            finish_response("stopped", confidence=0.9),
        ]
    )
    runner = AgentRunner(model=model, tools=registry)
    objective = AgentObjective(
        objective="ship it",
        allowed_actions=["read_repo"],
        restricted_actions=["deploy_prod"],
    )

    result = runner.run(objective)

    assert called["deploy"] is False  # handler never invoked
    deny_steps = [
        s
        for s in result.steps
        if s.decision is not None and s.decision.effect is DecisionEffect.DENY
    ]
    assert len(deny_steps) == 1
    assert deny_steps[0].decision is not None
    assert deny_steps[0].decision.matched_rule == "restricted_actions"
    # Default handoff rule escalates on policy conflict.
    assert result.needs_human is True
    assert result.status is RunStatus.ESCALATED


def test_agents_md_enters_model_context(tmp_path: Path) -> None:
    marker = "FORGE-AGENTS-MARKER-XYZ always write tests first"
    (tmp_path / "AGENTS.md").write_text(f"# Repo rules\n{marker}\n")

    model = ScriptedModelClient([finish_response("done", confidence=0.9)])
    runner = AgentRunner(model=model, repo_root=tmp_path)
    objective = AgentObjective(objective="do work")

    result = runner.run(objective)

    assert result.status is RunStatus.SUCCEEDED
    # The recorded first request's system prompt must contain the AGENTS.md text.
    assert model.requests, "model should have been called"
    assert marker in (model.requests[0].system or "")
    assert result.artifacts.get("agents_md_loaded") is True


def test_low_confidence_triggers_handoff() -> None:
    model = ScriptedModelClient([finish_response("unsure result", confidence=0.4)])
    runner = AgentRunner(model=model)
    objective = AgentObjective(
        objective="risky task",
        handoff_rules=HandoffRules(confidence_below=0.72),
    )

    result = runner.run(objective)

    assert result.confidence == 0.4
    assert result.needs_human is True
    assert result.status is RunStatus.ESCALATED


def test_model_stop_without_finish_uses_content_as_output() -> None:
    from forge_contracts import ModelResponse

    model = ScriptedModelClient([ModelResponse(content="plain answer", stop_reason="end_turn")])
    runner = AgentRunner(model=model)
    result = runner.run(AgentObjective(objective="answer a question"))
    assert result.output == "plain answer"
    assert result.status is RunStatus.SUCCEEDED


def test_max_iterations_escalates() -> None:
    registry = ToolRegistry()
    registry.add("loop_tool", lambda _a: ToolResult(ok=True, output="again"), action="read_repo")

    # Model always asks for another tool call -> never finishes.
    always_tool = tool_response("loop_tool", {"action": "read_repo"})
    model = ScriptedModelClient([], default=always_tool)
    runner = AgentRunner(model=model, tools=registry, max_iterations=3)
    objective = AgentObjective(objective="infinite", allowed_actions=["read_repo"])

    result = runner.run(objective)

    assert result.needs_human is True
    assert result.status is RunStatus.ESCALATED
    assert result.error == "max_iterations_exceeded"
    # iteration cap respected
    tool_calls = [s for s in result.steps if s.kind is StepKind.TOOL_CALL]
    assert len(tool_calls) <= 3


def test_worktree_used_when_enabled(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("git") is None:  # pragma: no cover - env without git
        import pytest

        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True
    )
    (repo / "AGENTS.md").write_text("worktree-marker-123")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "i"], check=True, capture_output=True)

    from forge_contracts import RepoTarget

    model = ScriptedModelClient([finish_response("done", confidence=0.9)])
    runner = AgentRunner(model=model, repo_root=repo, use_worktree=True)
    objective = AgentObjective(
        objective="work in sandbox",
        repo_targets=[RepoTarget(repo="local", base_branch="main", worktree=True)],
    )

    result = runner.run(objective)

    assert result.status is RunStatus.SUCCEEDED
    assert "worktree-marker-123" in (model.requests[0].system or "")
    # Worktree must be cleaned up after the run.
    assert result.artifacts.get("worktree_cleaned") is True
