"""Hardening tests for the agent runtime: escalation, retry, cleanup, worktree.

These exercise the error/escalation/retry/cleanup/worktree-failure branches of
:class:`forge_agent.runtime.AgentRunner` that the happy-path integration tests in
``test_runtime.py`` do not reach. All hermetic: scripted/raising fake models, no
network, no live model provider, no git required (the worktree failure paths use
a monkeypatched sandbox).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from forge_agent import runtime as runtime_mod
from forge_agent.runtime import AgentRunner, _base_branch, _branch_name
from forge_agent.sandbox import SandboxError
from forge_agent.state import AgentState
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_agent.tools import ToolRegistry, ToolResult
from forge_contracts import (
    AgentObjective,
    Decision,
    DecisionEffect,
    HandoffRules,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    RepoTarget,
    RunStatus,
    ToolCall,
)


class _ApprovalGate:
    """A gate that always requires human approval (REQUIRES_APPROVAL effect)."""

    def evaluate(self, call: ToolCall, objective: AgentObjective) -> Decision:
        return Decision(
            effect=DecisionEffect.REQUIRES_APPROVAL,
            reason="needs human approval",
            matched_rule="approval",
        )


def test_requires_approval_escalates_and_skips_dispatch() -> None:
    registry = ToolRegistry()
    called = {"hit": False}

    def touch(_args: dict[str, object]) -> ToolResult:
        called["hit"] = True
        return ToolResult(ok=True, output="done")

    registry.add("touch", touch, action="write_code")

    model = ScriptedModelClient(
        [
            tool_response("touch", {"action": "write_code"}),
            finish_response("attempted", confidence=0.95),
        ]
    )
    runner = AgentRunner(model=model, tools=registry, gate=_ApprovalGate())
    result = runner.run(AgentObjective(objective="modify code"))

    assert called["hit"] is False  # approval-gated tool never dispatched
    assert result.needs_human is True
    assert result.status is RunStatus.ESCALATED


def test_instructions_are_appended_to_first_user_message() -> None:
    model = ScriptedModelClient([finish_response("done", confidence=0.9)])
    runner = AgentRunner(model=model)
    runner.run(AgentObjective(objective="main goal", instructions="follow these steps"))

    assert model.requests
    first_user = model.requests[0].messages[-1].content or ""
    assert "main goal" in first_user
    assert "follow these steps" in first_user


def test_tool_failure_escalates_via_handoff_rules() -> None:
    registry = ToolRegistry()

    def run_tests(_args: dict[str, object]) -> ToolResult:
        return ToolResult(ok=False, error="tests failed")

    registry.add("run_tests", run_tests, action="read_repo")

    model = ScriptedModelClient(
        [
            tool_response("run_tests", {"action": "read_repo"}),
            finish_response("attempted a fix", confidence=0.95),
        ]
    )
    runner = AgentRunner(model=model, tools=registry)
    objective = AgentObjective(
        objective="fix the tests",
        allowed_actions=["read_repo"],
        # confidence is high, so only the test-failure rule can escalate here.
        handoff_rules=HandoffRules(confidence_below=0.1, on_test_failure_after_retries=0),
    )

    result = runner.run(objective)

    assert result.needs_human is True
    assert result.status is RunStatus.ESCALATED
    risks = result.artifacts.get("risks", [])
    assert any("tool failed" in r for r in risks)


def test_finish_can_request_human_and_attach_risks() -> None:
    model = ScriptedModelClient(
        [finish_response("done", confidence=0.9, needs_human=True, risks=["data loss possible"])]
    )
    runner = AgentRunner(model=model)
    result = runner.run(AgentObjective(objective="risky op"))

    assert result.needs_human is True
    assert result.status is RunStatus.ESCALATED
    assert "data loss possible" in result.artifacts.get("risks", [])


def test_route_finalizes_when_nothing_is_pending() -> None:
    # Defensive router contract: with no finish signal and no pending tool calls
    # there is nothing left to act on, so routing terminates the loop. (Through
    # the public ``run`` loop this state never arises, since a model turn always
    # yields either a finish or at least one pending call.)
    runner = AgentRunner(model=ScriptedModelClient([]))
    state = AgentState(objective=AgentObjective(objective="x"))
    state.finished = False
    state.pending = []
    assert runner._route(state) == "finalize"


def test_to_result_maps_error_without_human_to_failed() -> None:
    # The only runtime path that sets ``error`` also escalates, so drive the
    # error -> FAILED mapping directly through the result assembler.
    runner = AgentRunner(model=ScriptedModelClient([]))
    state = AgentState(objective=AgentObjective(objective="x"))
    state.error = "boom"
    state.needs_human = False

    result = runner._to_result(state)

    assert result.status is RunStatus.FAILED
    assert result.error == "boom"


# ---------------------------------------------------------------------------- #
# Worktree lifecycle (monkeypatched sandbox — no git required)                 #
# ---------------------------------------------------------------------------- #
def _worktree_objective() -> AgentObjective:
    return AgentObjective(
        objective="work in sandbox",
        repo_targets=[RepoTarget(repo="local", base_branch="main", worktree=True)],
    )


def test_worktree_creation_error_falls_back_to_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _FailingSandbox:
        def __init__(self, repo_root: object, *, base_branch: str = "main") -> None:
            self._created = False

        def create(self, branch: str | None = None) -> Path:
            raise SandboxError("cannot create worktree")

        def cleanup(self) -> None:  # pragma: no cover - never reached
            raise AssertionError("cleanup must not run after a failed create")

    monkeypatch.setattr(runtime_mod, "WorktreeSandbox", _FailingSandbox)

    model = ScriptedModelClient([finish_response("done", confidence=0.9)])
    runner = AgentRunner(model=model, repo_root=tmp_path, use_worktree=True)
    result = runner.run(_worktree_objective())

    assert result.status is RunStatus.SUCCEEDED
    assert result.artifacts.get("worktree_error") == "cannot create worktree"
    assert "worktree_cleaned" not in result.artifacts


def test_worktree_cleaned_up_on_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cleanups: list[bool] = []

    class _SpySandbox:
        def __init__(self, repo_root: object, *, base_branch: str = "main") -> None:
            self._root = Path(str(repo_root))

        def create(self, branch: str | None = None) -> Path:
            return self._root

        def cleanup(self) -> None:
            cleanups.append(True)

    class _BoomModel:
        def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("model exploded")

        def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]:
            yield ModelStreamEvent(type="text")

    monkeypatch.setattr(runtime_mod, "WorktreeSandbox", _SpySandbox)

    runner = AgentRunner(model=_BoomModel(), repo_root=tmp_path, use_worktree=True)
    with pytest.raises(RuntimeError, match="model exploded"):
        runner.run(_worktree_objective())

    # The ``finally`` block must remove the worktree even when the run blew up.
    assert cleanups == [True]


def test_worktree_success_cleans_up_and_records_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cleanups: list[bool] = []
    work_dir = tmp_path / "tree"
    work_dir.mkdir()

    class _OkSandbox:
        def __init__(self, repo_root: object, *, base_branch: str = "main") -> None:
            self.base_branch = base_branch

        def create(self, branch: str | None = None) -> Path:
            return work_dir

        def cleanup(self) -> None:
            cleanups.append(True)

    monkeypatch.setattr(runtime_mod, "WorktreeSandbox", _OkSandbox)

    model = ScriptedModelClient([finish_response("done", confidence=0.9)])
    runner = AgentRunner(model=model, repo_root=tmp_path, use_worktree=True)
    result = runner.run(_worktree_objective())

    assert result.status is RunStatus.SUCCEEDED
    assert result.artifacts.get("worktree_path") == str(work_dir)
    assert result.artifacts.get("worktree_cleaned") is True
    assert cleanups == [True]  # cleaned exactly once (normal path, not finally)


# ---------------------------------------------------------------------------- #
# Branch-naming helpers                                                         #
# ---------------------------------------------------------------------------- #
def test_base_branch_defaults_to_main_without_worktree_target() -> None:
    assert _base_branch(AgentObjective(objective="x")) == "main"


def test_base_branch_uses_first_worktree_target() -> None:
    objective = AgentObjective(
        objective="x",
        repo_targets=[RepoTarget(repo="r", base_branch="develop", worktree=True)],
    )
    assert _base_branch(objective) == "develop"


def test_branch_name_uses_branch_prefix_when_present() -> None:
    objective = AgentObjective(
        objective="x",
        repo_targets=[RepoTarget(repo="r", worktree=True, branch_prefix="feature")],
    )
    name = _branch_name(objective)
    assert name.startswith("feature-")


def test_branch_name_falls_back_to_key() -> None:
    objective = AgentObjective(
        objective="x",
        key="TASK-7",
        repo_targets=[RepoTarget(repo="r", worktree=True)],
    )
    assert _branch_name(objective).startswith("forge/TASK-7-")


def test_branch_name_falls_back_to_task_without_key() -> None:
    assert _branch_name(AgentObjective(objective="x")).startswith("forge/task-")
