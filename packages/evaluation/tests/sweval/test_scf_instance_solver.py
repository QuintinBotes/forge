"""The adapter that drives a running SCF instance as a bench solver (#197).

The instance is a stubbed HTTP transport; the repository it pushed to is a local git repo.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from scf_agent.sandbox import LocalSandboxProvider
from scf_eval.golden import GoldenCase
from scf_eval.sweval import BenchRunReport, CaseOutcome, ScfInstanceSolver, run_suite

SUITE = (
    Path(__file__).resolve().parents[2]
    / "scf_eval"
    / "benchmarks"
    / "forge-factory-bench"
    / "0.1.0"
)

_BASE = "def add(a, b):\n    return a - b\n"
_FIXED = "def add(a, b):\n    return a + b\n"
_CASE = GoldenCase(id="case-1", query="add() subtracts", expected_ids=["REQ-1"], kind="agent_task")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repository(tmp_path: Path) -> Path:
    """A clone whose origin has the case's base branch and the branch the run pushed."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "bench/case-1")
    (origin / "calc.py").write_text(_BASE, encoding="utf-8")
    (origin / "gone.py").write_text("x = 1\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "base")
    _git(origin, "checkout", "-q", "-b", "scf/case-1")
    (origin / "calc.py").write_text(_FIXED, encoding="utf-8")
    (origin / "NOTES.md").write_text("fixed\n", encoding="utf-8")
    _git(origin, "rm", "-q", "gone.py")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "fix")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    return tmp_path / "clone"


def _solver(
    clone: Path,
    responses: list[httpx.Response],
    sent: list[httpx.Request],
    base_url: str = "http://scf.test/api/v1",
) -> ScfInstanceSolver:
    replies = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return next(replies)

    return ScfInstanceSolver(
        base_url=base_url,
        token="test-token",
        repository="acme/bench",
        clone_dir=clone,
        model="model-a",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def test_submits_the_issue_and_collects_the_pushed_change(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []
    finished = {
        "status": "succeeded",
        "artifacts": {"branch": "scf/case-1", "model_usage": {"cost_usd": 0.42}},
    }
    solver = _solver(
        _repository(tmp_path),
        [
            httpx.Response(202, json={"run_id": "run-1", "status": "pending"}),
            httpx.Response(404),  # a governed run's agent run is not created yet
            httpx.Response(200, json={"status": "running"}),
            httpx.Response(200, json=finished),
        ],
        sent,
    )

    assert solver(_CASE) == {"calc.py": _FIXED, "NOTES.md": "fixed\n"}
    assert solver.last_cost_usd == 0.42
    assert solver.models == ("model-a",)

    post = sent[0]
    assert (post.method, post.url.path) == ("POST", "/api/v1/agent/runs")
    assert post.headers["Authorization"] == "Bearer test-token"
    body = json.loads(post.content)
    assert body["objective"] == "add() subtracts"
    assert body["model"] == "model-a"
    assert body["repo_targets"] == [
        {"repo": "acme/bench", "base_branch": "bench/case-1", "role": "primary"}
    ]
    assert [r.url.path for r in sent[1:]] == ["/api/v1/agent/runs/run-1"] * 3


def test_a_run_that_did_not_succeed_yields_an_empty_patch(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []
    solver = _solver(
        tmp_path / "no-clone-needed",
        [
            httpx.Response(202, json={"run_id": "run-2"}),
            httpx.Response(200, json={"status": "failed", "artifacts": {}}),
        ],
        sent,
    )
    assert solver(_CASE) == {}
    assert solver.last_cost_usd is None


def test_a_run_with_no_commits_yields_an_empty_patch(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []
    finished = {
        "status": "succeeded",
        "artifacts": {"branch": "scf/case-1", "pull_request": {"status": "no_changes"}},
    }
    solver = _solver(
        tmp_path / "never-fetched",
        [httpx.Response(202, json={"run_id": "run-3"}), httpx.Response(200, json=finished)],
        sent,
    )
    assert solver(_CASE) == {}


def test_a_refused_run_raises_naming_the_reason(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []
    refused = httpx.Response(409, json={"detail": "governed_run_requires_task"})
    solver = _solver(tmp_path, [refused], sent)
    with pytest.raises(RuntimeError, match="governed_run_requires_task"):
        solver(_CASE)


def test_refused_runs_are_infrastructure_errors_kept_apart_per_instance(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    refusal = {"detail": "ungoverned_run_refused: development only"}

    def run(base_url: str) -> BenchRunReport:
        solver = _solver(tmp_path, [httpx.Response(409, json=refusal)] * 5, [], base_url)
        return asyncio.run(
            run_suite(SUITE, solver, sandbox_provider=LocalSandboxProvider(), results_path=results)
        )

    report = run("http://scf.test/api/v1")
    for case in report.cases:
        assert case.outcome is CaseOutcome.INFRASTRUCTURE_ERROR
        assert "ungoverned_run_refused" in (case.reason or "")
    with pytest.raises(ValueError, match="another"):
        run("http://other.test/api/v1")
