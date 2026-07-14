"""Offline tests for the production Self-Eval runner (A3).

No network, no live model: a scripted model client authors the fix through the
coder tools, real git worktrees are checked out from a temp repo, and the
hidden tests run in the local ``worktree`` sandbox. Proves the agent -> file-map
adapter and the end-to-end scorecard without any BYOK credentials.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from forge_agent.sandbox import LocalSandboxProvider
from forge_agent.testing import ScriptedModelClient, finish_response, tool_response
from forge_eval.golden import GoldenCase
from forge_eval.sweval import SelfEvalScorecard
from forge_worker.self_eval_run import (
    ProductionEvalRunner,
    SelfEvalSuiteHandle,
    agent_solve,
    build_coder_tools,
    execute_self_eval_run,
)

_BROKEN = "def add(a, b):\n    return a - b\n"
_FIXED = "def add(a, b):\n    return a + b\n"
_TEST = "from mymod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_KEEP = "def test_keep():\n    assert True\n"

_FTP = "test_mymod.py::test_add"
_PTP = "test_keep.py::test_keep"


def _init_repo(root: Path) -> str:
    """Create a git repo with the broken module + hidden tests; return base sha."""
    (root / "mymod.py").write_text(_BROKEN, encoding="utf-8")
    (root / "test_mymod.py").write_text(_TEST, encoding="utf-8")
    (root / "test_keep.py").write_text(_KEEP, encoding="utf-8")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "selfeval@forge.test")
    git("config", "user.name", "Self Eval")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    return root


def _scripted_writer(path: str, content: str) -> ScriptedModelClient:
    """A model that writes one file then finishes."""
    return ScriptedModelClient(
        responses=[tool_response("write_file", {"path": path, "content": content})],
        default=finish_response("done", confidence=0.9),
    )


# --------------------------------------------------------------------------- #
# Coder tools                                                                   #
# --------------------------------------------------------------------------- #


def _run_tool(tools, name: str, args: dict) -> object:
    tool = tools.get(name)
    assert tool is not None, f"tool {name!r} not registered"
    return tool.run(args)


def test_coder_tools_read_write_list(tmp_path: Path) -> None:
    tools = build_coder_tools(tmp_path)
    (tmp_path / "a.py").write_text("hello", encoding="utf-8")

    read = _run_tool(tools, "read_file", {"path": "a.py"})
    assert read.ok and read.output == "hello"

    wrote = _run_tool(tools, "write_file", {"path": "sub/b.py", "content": "x = 1"})
    assert wrote.ok
    assert (tmp_path / "sub" / "b.py").read_text() == "x = 1"

    listed = _run_tool(tools, "list_files", {"path": "."})
    assert "a.py" in listed.output and "sub/b.py" in listed.output


def test_coder_tools_reject_path_traversal(tmp_path: Path) -> None:
    tools = build_coder_tools(tmp_path)
    with pytest.raises(ValueError, match="escapes worktree"):
        _run_tool(tools, "write_file", {"path": "../evil.py", "content": "nope"})


# --------------------------------------------------------------------------- #
# agent_solve — the agent -> file-map adapter                                   #
# --------------------------------------------------------------------------- #


def _case(base: str) -> GoldenCase:
    return GoldenCase(
        id="swe-1",
        query="make add() correct",
        expected_ids=[_FTP],
        kind="agent_task",
        metadata={"fail_to_pass": [_FTP], "pass_to_pass": [_PTP], "base_commit": base},
    )


def test_agent_solve_returns_agent_edits(repo: Path) -> None:
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    patch = agent_solve(
        _case(base),
        model_client=_scripted_writer("mymod.py", _FIXED),
        repo_path=str(repo),
    )
    assert patch == {"mymod.py": _FIXED}


def test_agent_solve_empty_when_agent_edits_nothing(repo: Path) -> None:
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # A model that just finishes without writing produces no patch (honest miss).
    idle = ScriptedModelClient(responses=[], default=finish_response("nothing to do"))
    assert agent_solve(_case(base), model_client=idle, repo_path=str(repo)) == {}


# --------------------------------------------------------------------------- #
# ProductionEvalRunner                                                          #
# --------------------------------------------------------------------------- #


def _write_suite(version_dir: Path, base: str) -> None:
    """A minimal non-frozen suite dir with one minted SWE case."""
    version_dir.mkdir(parents=True)
    (version_dir / "cases").mkdir()
    (version_dir / "cases" / "self_eval.json").write_text(
        __import__("json").dumps(
            [
                {
                    "id": "swe-1",
                    "query": "make add() correct",
                    "expected_ids": [_FTP],
                    "kind": "agent_task",
                    "metadata": {
                        "fail_to_pass": [_FTP],
                        "pass_to_pass": [_PTP],
                        "base_commit": base,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    (version_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "slug": "self-eval",
                "version": "1.0.0",
                "title": "Private self-eval suite",
                "schema_version": 1,
                "frozen": False,
                "scoring": {
                    "primary_metric": "agent.fail_to_pass_rate",
                    "metric_weights": {"agent.fail_to_pass_rate": 1.0},
                },
                "case_files": ["cases/self_eval.json"],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_runner_cold_start_no_suite() -> None:
    runner = ProductionEvalRunner(
        resolve_suite=lambda _ws: None,
        model_client_for=lambda _cfg: _scripted_writer("mymod.py", _FIXED),
        sandbox_provider=LocalSandboxProvider(),
    )
    assert await runner(uuid.uuid4(), {"model": "x"}) is None


@pytest.mark.asyncio
async def test_runner_offline_no_model(repo: Path, tmp_path: Path) -> None:
    version_dir = tmp_path / "suite"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    _write_suite(version_dir, base)
    handle = SelfEvalSuiteHandle(uuid.uuid4(), str(version_dir), str(repo))
    runner = ProductionEvalRunner(
        resolve_suite=lambda _ws: handle,
        model_client_for=lambda _cfg: None,  # offline / no BYOK
        sandbox_provider=LocalSandboxProvider(),
    )
    assert await runner(uuid.uuid4(), {"model": "x"}) is None


@pytest.mark.asyncio
async def test_runner_scores_end_to_end(repo: Path, tmp_path: Path) -> None:
    version_dir = tmp_path / "suite"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    _write_suite(version_dir, base)
    handle = SelfEvalSuiteHandle(uuid.uuid4(), str(version_dir), str(repo))
    runner = ProductionEvalRunner(
        resolve_suite=lambda _ws: handle,
        model_client_for=lambda _cfg: _scripted_writer("mymod.py", _FIXED),
        sandbox_provider=LocalSandboxProvider(),
    )
    card = await runner(uuid.uuid4(), {"model": "claude-opus"})
    assert card is not None
    assert (card.total, card.resolved) == (1, 1)
    assert card.resolution_rate == 1.0


@pytest.mark.asyncio
async def test_runner_wrong_config_does_not_resolve(repo: Path, tmp_path: Path) -> None:
    version_dir = tmp_path / "suite"
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    _write_suite(version_dir, base)
    handle = SelfEvalSuiteHandle(uuid.uuid4(), str(version_dir), str(repo))
    # A config whose agent writes a still-broken module must score 0 — the signal
    # the gate blocks a regressing config on.
    runner = ProductionEvalRunner(
        resolve_suite=lambda _ws: handle,
        model_client_for=lambda _cfg: _scripted_writer("mymod.py", _BROKEN),
        sandbox_provider=LocalSandboxProvider(),
    )
    card = await runner(uuid.uuid4(), {"model": "cheap"})
    assert card is not None
    assert (card.total, card.resolved) == (1, 0)
    assert card.resolution_rate == 0.0


# --------------------------------------------------------------------------- #
# execute_self_eval_run — run -> record baseline (A4)                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_execute_records_baseline() -> None:
    calls: list[dict] = []

    async def runner(_ws: uuid.UUID, _cfg: object) -> SelfEvalScorecard:
        return SelfEvalScorecard(total=10, resolved=8, resolution_rate=0.8)

    def record(**kwargs: object) -> None:
        calls.append(kwargs)

    ws, suite = uuid.uuid4(), uuid.uuid4()
    card = await execute_self_eval_run(
        workspace_id=ws,
        proposed_config={"model": "claude-opus", "scope": "ao.role_config"},
        benchmark_suite_id=suite,
        runner=runner,
        record_baseline=record,
    )
    assert card is not None and card.resolution_rate == 0.8
    assert len(calls) == 1
    assert calls[0]["workspace_id"] == ws
    assert calls[0]["benchmark_suite_id"] == suite
    assert calls[0]["resolution_rate"] == 0.8
    assert calls[0]["config"] == {"model": "claude-opus", "scope": "ao.role_config"}


@pytest.mark.asyncio
async def test_execute_records_nothing_when_runner_returns_none() -> None:
    recorded: list[object] = []

    async def runner(_ws: uuid.UUID, _cfg: object) -> None:
        return None  # no suite / no cases / offline

    card = await execute_self_eval_run(
        workspace_id=uuid.uuid4(),
        proposed_config={"model": "x"},
        benchmark_suite_id=uuid.uuid4(),
        runner=runner,
        record_baseline=lambda **kw: recorded.append(kw),
    )
    assert card is None
    assert recorded == []  # a no-score run never writes a baseline


@pytest.mark.asyncio
async def test_execute_records_via_real_service() -> None:
    """End-to-end with the real SelfEvalService (in-memory) — baseline reads back."""
    from sqlalchemy import StaticPool, create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from forge_api.services.self_eval_service import SelfEvalService
    from forge_db.base import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    service = SelfEvalService(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )

    async def runner(_ws: uuid.UUID, _cfg: object) -> SelfEvalScorecard:
        return SelfEvalScorecard(total=4, resolved=3, resolution_rate=0.75)

    ws = uuid.uuid4()
    await execute_self_eval_run(
        workspace_id=ws,
        proposed_config={"model": "claude-opus"},
        benchmark_suite_id=uuid.uuid4(),
        runner=runner,
        record_baseline=service.record_baseline,
    )
    assert service.workspace_baseline(ws) == 0.75
