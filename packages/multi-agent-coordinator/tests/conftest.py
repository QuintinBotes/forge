"""Shared fixtures for the F27 coordinator tests.

Everything here is hermetic: no network, no live LLM. The scripted subagent fake
and objective builders live in :mod:`_helpers` (imported as a plain module).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _helpers import ScriptingHub, _git

from forge_contracts import SubagentRules
from forge_coordinator import (
    CoordinatorDeps,
    CoordinatorSettings,
    InMemorySubAgentRunSink,
    Supervisor,
)


@pytest.fixture
def hub() -> ScriptingHub:
    return ScriptingHub()


@pytest.fixture
def settings() -> CoordinatorSettings:
    return CoordinatorSettings(enabled=True, max_parallel_cap=4, confidence_threshold=0.72)


@pytest.fixture
def sink() -> InMemorySubAgentRunSink:
    return InMemorySubAgentRunSink()


@pytest.fixture
def make_supervisor(
    hub: ScriptingHub, settings: CoordinatorSettings, sink: InMemorySubAgentRunSink
):
    def _make(**overrides) -> Supervisor:
        deps = CoordinatorDeps(
            agent_factory=hub.agent_factory,
            settings=overrides.pop("settings", settings),
            sub_agent_sink=overrides.pop("sub_agent_sink", sink),
            **overrides,
        )
        return Supervisor(deps)

    return _make


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "test@forge.dev")
    _git(repo, "config", "user.name", "Forge Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("# base\n")
    (repo / "AGENTS.md").write_text("Be careful.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "baseline")
    return repo


@pytest.fixture
def allow_all_rules() -> SubagentRules:
    return SubagentRules(
        allow_subagents=True,
        allowed_roles=[
            "planner",
            "researcher",
            "implementer",
            "tester",
            "reviewer",
            "security",
        ],
        max_parallel=2,
    )


@pytest.fixture
def deny_rules() -> SubagentRules:
    return SubagentRules(allow_subagents=False, allowed_roles=[], max_parallel=0)
